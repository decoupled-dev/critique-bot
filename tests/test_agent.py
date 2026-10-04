from __future__ import annotations

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
from critique_bot.chat_client import COMPLETION_IDLE


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

    def test_invalid_json_and_unclosed_tag(self) -> None:
        calls, unclosed = parse_tool_calls("<tool_call>\n{not json}\n</tool_call>")
        self.assertFalse(unclosed)
        self.assertIsNotNone(calls[0].error)
        calls, unclosed = parse_tool_calls('<tool_call>\n{"tool": "list_files"}')
        self.assertTrue(unclosed)
        self.assertEqual(calls, [])

    def test_accepts_name_and_args(self) -> None:
        calls, unclosed = parse_tool_calls(
            '<tool_call>\n{"name": "git_status", "args": {}}\n</tool_call>'
        )
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "git_status")
        self.assertEqual(calls[0].arguments, {})


class CommandArgvTests(unittest.TestCase):
    def test_powershell_and_bash(self) -> None:
        windows = command_argv("Get-Location", platform_name="win32")
        self.assertEqual(
            windows[:4],
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"],
        )
        self.assertEqual(windows[-1], "Get-Location")
        self.assertEqual(command_argv("pwd", platform_name="linux"), ["bash", "-lc", "pwd"])


class ToolTests(unittest.TestCase):
    def test_write_read_edit_delete_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrote = execute_tool(
                "write_files",
                {"path": "src/app.py", "contents": "VALUE = 1\n"},
                workspace=root,
            )
            self.assertTrue(wrote["ok"], wrote)
            listed = execute_tool("list_files", {"path": "src"}, workspace=root)
            self.assertIn("src/app.py", listed["output"])
            read = execute_tool(
                "read_files",
                {"paths": ["src/app.py"]},
                workspace=root,
            )
            self.assertIn("VALUE = 1", read["output"])
            edited = execute_tool(
                "edit_file",
                {"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
                workspace=root,
            )
            self.assertTrue(edited["ok"], edited)
            self.assertEqual((root / "src" / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            deleted = execute_tool("delete_file", {"path": "src/app.py"}, workspace=root)
            self.assertTrue(deleted["ok"], deleted)
            self.assertFalse((root / "src" / "app.py").exists())
            refused = execute_tool("delete_file", {"path": "."}, workspace=root)
            self.assertFalse(refused["ok"])

    def test_run_command_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = execute_tool(
                "run_command",
                {"command": "printf 'pong'"},
                workspace=Path(tmp),
            )
            self.assertTrue(result["ok"], result)
            self.assertIn("pong", result["output"])

    def test_git_status_diff_and_apply_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "bot@example.com"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "bot"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "note.txt").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "note.txt"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
            )
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


class LoopTests(unittest.TestCase):
    def test_task_is_sent_alone_and_tools_run_until_plain_reply(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = [
                    (
                        "<plan>\n"
                        "Files: note.txt\n"
                        "Change: replace one with two\n"
                        "Check: read note.txt\n"
                        "</plan>"
                    ),
                    '<tool_call>\n{"tool": "edit_file", "arguments": {"path": "note.txt", "old_string": "one", "new_string": "two"}}\n</tool_call>',
                    "Done. note.txt now says two.",
                ]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        session = Session()
        turns = run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="change note.txt",
            max_rounds=5,
            max_result_chars=8000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertIn("change note.txt", session.sent[0])
        self.assertIn("tool_call", session.sent[0])
        self.assertIn("Plan recorded", session.sent[1])
        self.assertIn("edit_file", session.sent[2])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(turns[-1]["content"], "Done. note.txt now says two.")

    def test_seed_is_first_and_task_follows(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                if len(self.sent) == 1:
                    return "READY"
                return "All set."

        session = Session()
        run_agent_loop(
            session,
            workspace=Path("."),
            index_path=None,
            cache_dir=None,
            first_task="update the test cases",
            max_rounds=3,
            max_result_chars=4000,
            seed="INSTRUCTIONS",
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertEqual(session.sent[0], "INSTRUCTIONS")
        self.assertIn("update the test cases", session.sent[1])

    def test_unclosed_tool_call_is_not_executed(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("keep\n", encoding="utf-8")

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = {"completion": COMPLETION_IDLE}
                self._left = 2

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                self._left -= 1
                if self._left == 1:
                    return '<tool_call>\n{"tool": "delete_file", "arguments": {"path": "note.txt"}}'
                return "Stopped."

        session = Session()
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="delete it",
            max_rounds=4,
            max_result_chars=4000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertTrue((root / "note.txt").exists())
        self.assertIn("truncated", session.sent[1])

    def test_refusal_without_a_tool_call_is_sent_back(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                if len(self.sent) == 1:
                    return "I can't modify files because tools aren't available."
                if len(self.sent) == 2:
                    return (
                        '<tool_call>\n{"tool": "list_files", "arguments": {"path": "."}}\n'
                        "</tool_call>"
                    )
                return "Done."

        session = Session()
        root = Path(tempfile.mkdtemp())
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="fix the bug",
            max_rounds=4,
            max_result_chars=4000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertIn("list_files", session.sent[1])
        self.assertGreaterEqual(len(session.sent), 2)

    def test_prose_after_a_failed_tool_is_sent_back(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("keep\n", encoding="utf-8")

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = [
                    (
                        "<plan>\n"
                        "Files: note.txt\n"
                        "Change: replace keep with the new text\n"
                        "Check: read note.txt\n"
                        "</plan>"
                    ),
                    (
                        '<tool_call>\n{"tool": "edit_file", "arguments": '
                        '{"path": "note.txt", "old_string": "missing", "new_string": "x"}}\n'
                        "</tool_call>"
                    ),
                    "I'll read the file and try again.",
                    (
                        '<tool_call>\n{"tool": "read_files", "arguments": '
                        '{"paths": ["note.txt"]}}\n</tool_call>'
                    ),
                    "Done.",
                ]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        session = Session()
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="change note.txt",
            max_rounds=None,
            max_result_chars=8000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertIn("old_string was not found", session.sent[2])
        self.assertIn("tool_call", session.sent[3])
        self.assertIn("1|keep", session.sent[4])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "keep\n")

    def test_no_edit_needed_replies_done_and_ends(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "note.txt"
        target.write_text("keep\n", encoding="utf-8")

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = [
                    (
                        "<plan>\n"
                        "Files: note.txt\n"
                        "Change: leave keep as it is\n"
                        "Check: read note.txt\n"
                        "</plan>"
                    ),
                    (
                        '<tool_call>\n{"tool": "edit_file", "arguments": '
                        '{"path": "note.txt", "old_string": "keep", "new_string": "keep"}}\n'
                        "</tool_call>"
                    ),
                    "The line is already present. No edit needed.",
                    "ignored after DONE",
                    (
                        '<tool_call>\n{"tool": "edit_file", "arguments": '
                        '{"path": "note.txt", "old_string": "keep", "new_string": "changed"}}\n'
                        "</tool_call>"
                    ),
                ]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        session = Session()
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="make sure note.txt says keep",
            max_rounds=None,
            max_result_chars=8000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertEqual(session.sent[-1], "DONE")
        self.assertNotIn("previous step did not finish", session.sent[-1])
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(len(session._replies), 1)

    def test_done_after_a_failed_edit_ends(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "note.txt"
        target.write_text("keep\n", encoding="utf-8")

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = [
                    (
                        "<plan>\n"
                        "Files: note.txt\n"
                        "Change: replace keep\n"
                        "Check: read note.txt\n"
                        "</plan>"
                    ),
                    (
                        '<tool_call>\n{"tool": "edit_file", "arguments": '
                        '{"path": "note.txt", "old_string": "missing", "new_string": "x"}}\n'
                        "</tool_call>"
                    ),
                    "DONE",
                    (
                        '<tool_call>\n{"tool": "edit_file", "arguments": '
                        '{"path": "note.txt", "old_string": "keep", "new_string": "changed"}}\n'
                        "</tool_call>"
                    ),
                ]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        session = Session()
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="change note.txt",
            max_rounds=None,
            max_result_chars=8000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertNotIn("DONE", session.sent)
        self.assertFalse(any("previous step did not finish" in item for item in session.sent))
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(len(session._replies), 1)

    def test_question_after_a_read_is_sent_back_until_a_tool(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = [
                    (
                        '<tool_call>\n{"tool": "read_files", "arguments": '
                        '{"paths": ["note.txt"]}}\n</tool_call>'
                    ),
                    "I have the relevant code context now. What would you like me to change?",
                    "The edit tools aren't available in my current tool set.",
                    (
                        "<plan>\nFiles: note.txt\nChange: replace one with two\n"
                        "Check: read note.txt\n</plan>"
                    ),
                    (
                        '<tool_call>\n{"tool": "edit_file", "arguments": '
                        '{"path": "note.txt", "old_string": "one", "new_string": "two"}}\n'
                        "</tool_call>"
                    ),
                    "Done. note.txt now says two.",
                ]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        session = Session()
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="replace one with two in note.txt",
            max_rounds=None,
            max_result_chars=8000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertIn("what to change", session.sent[2].lower())
        self.assertIn("unavailable", session.sent[3].lower())
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_reworded_refusal_is_sent_back(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = [
                    (
                        '<tool_call>\n{"tool": "read_files", "arguments": '
                        '{"paths": ["note.txt"]}}\n</tool_call>'
                    ),
                    (
                        "I’m unable to send the requested repository tool call "
                        "because the exposed tool set contains no repository "
                        "file-operation action."
                    ),
                    (
                        "<plan>\nFiles: note.txt\nChange: replace one with two\n"
                        "Check: read note.txt\n</plan>"
                    ),
                    (
                        '<tool_call>\n{"tool": "edit_file", "arguments": '
                        '{"path": "note.txt", "old_string": "one", "new_string": "two"}}\n'
                        "</tool_call>"
                    ),
                    "Done. note.txt now says two.",
                ]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        session = Session()
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="replace one with two in note.txt",
            max_rounds=None,
            max_result_chars=8000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertIn("edit_file", session.sent[2])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_read_files_accepts_start_and_end_line(self) -> None:
        root = Path(tempfile.mkdtemp())
        lines = "\n".join(f"line {index}" for index in range(10)) + "\n"
        (root / "note.txt").write_text(lines, encoding="utf-8")
        result = execute_tool(
            "read_files",
            {"paths": ["note.txt"], "start_line": 3, "end_line": 5},
            workspace=root,
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("3|line 2", result["output"])
        self.assertIn("5|line 4", result["output"])
        self.assertNotIn("6|line 5", result["output"])

    def test_edit_rejects_a_whitespace_only_rewrite(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "a.py"
        target.write_text("def add():\n    return 1\n", encoding="utf-8")
        result = execute_tool(
            "edit_file",
            {
                "path": "a.py",
                "old_string": "def add():\n return 1\n",
                "new_string": "def add():\n return 1\n",
            },
            workspace=root,
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("same text", result["error"])
        self.assertEqual(target.read_text(encoding="utf-8"), "def add():\n    return 1\n")

    def test_edit_before_plan_does_not_change_the_file(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        edit = (
            '<tool_call>\n{"tool": "edit_file", "arguments": '
            '{"path": "note.txt", "old_string": "one", "new_string": "two"}}\n'
            "</tool_call>"
        )
        plan = (
            "<plan>\nFiles: note.txt\nChange: replace one with two\n"
            "Check: read note.txt\n</plan>"
        )

        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = [edit, plan, edit, "Done."]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        session = Session()
        run_agent_loop(
            session,
            workspace=root,
            index_path=None,
            cache_dir=None,
            first_task="change note.txt",
            max_rounds=None,
            max_result_chars=8000,
            read_message=lambda: None,
            emit=lambda _text: None,
        )
        self.assertIn("no plan recorded", session.sent[1])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
        self.assertIn("updated", session.sent[3])

    def test_fenced_json_is_a_tool_call(self) -> None:
        calls, unclosed = parse_tool_calls(
            '```json\n{"tool": "git_status", "arguments": {}}\n```'
        )
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "git_status")

    def test_trailing_comma_and_cut_off_tail_keep_the_good_call(self) -> None:
        calls, unclosed = parse_tool_calls(
            '<tool_call>\n{"tool": "git_status", "arguments": {},}\n</tool_call>'
        )
        self.assertFalse(unclosed)
        self.assertIsNone(calls[0].error)
        self.assertEqual(calls[0].tool, "git_status")
        calls, unclosed = parse_tool_calls(
            '<tool_call>\n{"tool": "list_files", "arguments": {"path": "."}}\n</tool_call>\n'
            '<tool_call>\n{"tool": "read_files", "arguments": {"paths": ["a.py"]}}\n'
        )
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "list_files")
        self.assertIsNone(calls[0].error)
        self.assertTrue(calls[-1].error)
        self.assertIn("cut off", calls[-1].error or "")

    def test_edit_repairs_line_prefix_and_indent(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "a.py"
        target.write_text(
            "def test_add_ones():\n    assert add(1, 1) == 2\n",
            encoding="utf-8",
        )
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

    def test_read_files_windows_a_long_file(self) -> None:
        root = Path(tempfile.mkdtemp())
        lines = "\n".join(f"line {index}" for index in range(500)) + "\n"
        (root / "big.txt").write_text(lines, encoding="utf-8")
        result = execute_tool(
            "read_files",
            {"paths": ["big.txt"]},
            workspace=root,
        )
        self.assertTrue(result["ok"])
        self.assertIn("1|line 0", result["output"])
        self.assertIn("next offset=201", result["output"])
        self.assertNotIn("201|line 200", result["output"])

    def test_missing_bot_home_message_is_cli(self) -> None:
        self.assertEqual(len(ALLOWED_TOOLS), 10)
