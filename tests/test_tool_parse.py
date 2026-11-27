# SPDX-License-Identifier: Apache-2.0
"""Tool calls written as text (when the server's parser misses them) and schema checks."""

from __future__ import annotations

import json
import unittest

from critique_bot.tool_parse import recover_calls, validate_arguments


class RecoverCallsTests(unittest.TestCase):
    def test_hermes(self) -> None:
        text = 'Reading it.\n<tool_call>\n{"name": "read_files", "arguments": {"paths": ["a.py"]}}\n</tool_call>'
        calls, rest = recover_calls(text)
        self.assertEqual([(c.name, c.arguments, c.source) for c in calls], [("read_files", {"paths": ["a.py"]}, "hermes")])
        self.assertEqual(rest, "Reading it.")

    def test_qwen_xml_keeps_code_unescaped_and_the_last_duplicate(self) -> None:
        code = 'def f(x):\n    return "a\\\\b"\n'
        text = (
            "<tool_call>\n<function=edit_file>\n<parameter=path>\nsrc/a.py\n</parameter>\n"
            "<parameter=path>\nsrc/b.py\n</parameter>\n"
            f"<parameter=new_string>\n{code}</parameter>\n<parameter=replace_all>\nfalse\n</parameter>\n"
            "</function>\n</tool_call>"
        )
        calls, rest = recover_calls(text)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.name, "edit_file")
        self.assertEqual(call.arguments["path"], "src/b.py")
        self.assertEqual(call.arguments["new_string"], code.rstrip("\n"))
        self.assertIs(call.arguments["replace_all"], False)
        self.assertTrue(any("twice" in note for note in call.notes))
        self.assertEqual(rest, "")

    def test_mistral_fenced_and_bare_json(self) -> None:
        calls, _ = recover_calls('[TOOL_CALLS] [{"name": "git_status", "arguments": {}}]')
        self.assertEqual(calls[0].name, "git_status")
        calls, rest = recover_calls('Now:\n```json\n{"name": "find_files", "arguments": "{\\"glob\\": \\"*.py\\"}"}\n```')
        self.assertEqual((calls[0].name, calls[0].arguments), ("find_files", {"glob": "*.py"}))
        self.assertEqual(rest, "Now:")
        calls, _ = recover_calls(json.dumps([{"function": {"name": "a", "arguments": {}}}, {"name": "b", "arguments": {}}]))
        self.assertEqual([c.name for c in calls], ["a", "b"])

    def test_plain_text_and_broken_json_are_not_calls(self) -> None:
        for text in ("COMPLETED", "The value is {not json}", '<tool_call>{"name": "x", "arguments":', ""):
            calls, rest = recover_calls(text)
            self.assertEqual(calls, [], text)


class ValidateTests(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 4},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "files": {"type": "array", "items": {"type": "object", "required": ["path"],
                                                  "properties": {"path": {"type": "string"}}}},
        },
    }

    def test_good_arguments(self) -> None:
        self.assertEqual(validate_arguments({"path": "x", "depth": 2, "files": [{"path": "y"}]}, self.SCHEMA), [])

    def test_problems_are_specific(self) -> None:
        problems = validate_arguments({"depth": 9, "mode": "c", "files": [{}], "extra": 1}, self.SCHEMA)
        self.assertIn("argument 'path' is required", problems)
        self.assertIn("depth should be at most 4, got 9", problems)
        self.assertIn("mode should be one of ['a', 'b'], got 'c'", problems)
        self.assertIn("files[0].path is required", problems)
        self.assertEqual(validate_arguments({"path": True}, self.SCHEMA), ["path should be string, got bool"])
        self.assertEqual(validate_arguments({"path": "x", "depth": True}, self.SCHEMA), ["depth should be integer, got bool"])


if __name__ == "__main__":
    unittest.main()
