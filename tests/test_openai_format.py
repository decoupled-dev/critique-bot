"""The OpenAI Responses format as the tool protocol: parsing, results, prompts, and the loop."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot import agent, openai_format, tool_api
from critique_bot.agent import (
    MINIMAL_TOOLS,
    TOOL_FORMAT_OPENAI,
    _tool_setup,
    hide_tool_markup,
    load_prompt_sections,
    run_agent_loop,
    tool_format_of,
)

REPO = Path(__file__).resolve().parents[1]


def _call_item(name: str, call_id: str, **arguments: object) -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _reply(*items: dict) -> str:
    return json.dumps({"output": list(items)})


def _message(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


class ParseTests(unittest.TestCase):
    def test_output_wrapper_in_a_fence_with_words_around(self) -> None:
        raw = "Reading it.\n```json\n" + _reply(_call_item("read_files", "c1", paths=["a.py"])) + "\n```"
        parsed = openai_format.parse_reply(raw)
        assert parsed is not None
        self.assertEqual([(c.name, c.arguments, c.call_id) for c in parsed.calls], [("read_files", {"paths": ["a.py"]}, "c1")])
        self.assertEqual(parsed.text, "Reading it.")

    def test_arguments_as_an_object_and_missing_call_id(self) -> None:
        raw = json.dumps({"type": "function_call", "name": "list_files", "arguments": {"path": "."}})
        parsed = openai_format.parse_reply(raw)
        assert parsed is not None
        self.assertEqual(parsed.calls[0].arguments, {"path": "."})
        self.assertEqual(parsed.calls[0].call_id, "call_1")

    def test_message_items_are_the_words(self) -> None:
        parsed = openai_format.parse_reply(_reply(_message("All done."), _message("COMPLETED")))
        assert parsed is not None
        self.assertEqual(parsed.calls, [])
        self.assertEqual(parsed.text, "All done.\n\nCOMPLETED")

    def test_chat_completions_shapes(self) -> None:
        response = {"choices": [{"message": {"role": "assistant", "content": "Looking.", "tool_calls": [
            {"id": "t9", "type": "function", "function": {"name": "git_status", "arguments": "{}"}}]}}]}
        parsed = openai_format.parse_reply(json.dumps(response))
        assert parsed is not None
        self.assertEqual([(c.name, c.call_id) for c in parsed.calls], [("git_status", "t9")])
        self.assertEqual(parsed.text, "Looking.")

    def test_made_up_results_are_cut(self) -> None:
        raw = _reply(
            _call_item("read_files", "c1", path="a.py"),
            {"type": "function_call_output", "call_id": "c1", "output": "fake"},
            _call_item("delete_file", "c2", path="a.py"),
        )
        parsed = openai_format.parse_reply(raw)
        assert parsed is not None
        self.assertTrue(parsed.fabricated)
        self.assertEqual([c.name for c in parsed.calls], ["read_files"])

    def test_cut_off_call_is_unclosed(self) -> None:
        raw = '{"output": [{"type": "function_call", "call_id": "c1", "name": "write_files", "arguments": "{\\"path\\": \\"a.py'
        parsed = openai_format.parse_reply(raw, complete=False)
        assert parsed is not None
        self.assertTrue(parsed.unclosed)
        self.assertEqual(parsed.calls, [])

    def test_a_reply_repeated_on_the_page_runs_each_call_once(self) -> None:
        once = _reply(_call_item("read_files", "c1", paths=["a.py"]))
        parsed = openai_format.parse_reply("Checking.\n" + once + once)
        assert parsed is not None
        self.assertEqual([c.call_id for c in parsed.calls], ["c1"])

    def test_same_tool_with_other_ids_still_runs_twice(self) -> None:
        parsed = openai_format.parse_reply(_reply(
            _call_item("run_command", "c1", command="make"), _call_item("run_command", "c2", command="make")))
        assert parsed is not None
        self.assertEqual(len(parsed.calls), 2)

    def test_empty_output_is_an_empty_reply_not_an_answer(self) -> None:
        parsed = openai_format.parse_reply('{"output":[]}')
        assert parsed is not None
        self.assertEqual((parsed.text, parsed.calls), ("", []))

    def test_instruction_has_a_parseable_example(self) -> None:
        example = openai_format.INSTRUCTION.rsplit("\n", 1)[1]
        parsed = openai_format.parse_reply(example)
        assert parsed is not None
        self.assertEqual(parsed.calls[0].name, "read_files")

    def test_bad_arguments_become_a_call_error(self) -> None:
        raw = json.dumps({"type": "function_call", "call_id": "c1", "name": "read_files", "arguments": "{not json"})
        parsed = openai_format.parse_reply(raw)
        assert parsed is not None
        self.assertIn("arguments must be a JSON object", parsed.calls[0].error or "")

    def test_plain_text_and_tag_calls_are_not_this_format(self) -> None:
        self.assertIsNone(openai_format.parse_reply("Just words, COMPLETED"))
        self.assertIsNone(openai_format.parse_reply('<tool_call>\n{"tool": "list_files"}\n</tool_call>'))
        self.assertIsNone(openai_format.parse_reply('```json\n{"risk": "safe", "comments": []}\n```'))

    def test_visible_text_for_transcripts(self) -> None:
        self.assertEqual(hide_tool_markup(_reply(_message("Fixed it."), _call_item("git_status", "c1"))), "Fixed it.")


class ResultTests(unittest.TestCase):
    def test_results_carry_their_call_ids(self) -> None:
        body = json.loads(openai_format.format_results([
            {"tool": "read_files", "ok": True, "output": "1|x", "call_id": "c1"},
            {"tool": "run_command", "ok": False, "error": "denied by the user"},
        ]))
        first, second = body["input"]
        self.assertEqual((first["type"], first["call_id"]), ("function_call_output", "c1"))
        self.assertEqual(first["output"], {"tool": "read_files", "ok": True, "output": "1|x"})
        self.assertEqual(second["call_id"], "call_2")
        self.assertEqual(second["output"]["error"], "denied by the user")

    def test_tools_block_is_flat_responses_tools(self) -> None:
        block = openai_format.tools_block(tool_api.registry().openai_tools())
        payload = json.loads(block.split("\n", 1)[1])
        first = payload["tools"][0]
        self.assertEqual(first["type"], "function")
        self.assertIn("name", first)
        self.assertIn("parameters", first)
        self.assertNotIn("function", first)


class PromptTests(unittest.TestCase):
    def test_overlay_only_overrides_known_sections(self) -> None:
        base = load_prompt_sections()
        openai = load_prompt_sections(TOOL_FORMAT_OPENAI)
        self.assertEqual(openai["_FORMAT"], TOOL_FORMAT_OPENAI)
        self.assertEqual(base["_FORMAT"], "tags")
        overlay = agent._read_sections(agent._agent_prompt_path("agent_openai.txt"))
        self.assertEqual(sorted(set(overlay) - set(base)), [])

    def test_openai_prompts_never_mention_tag_blocks(self) -> None:
        sections = load_prompt_sections(TOOL_FORMAT_OPENAI)
        overlay = agent._read_sections(agent._agent_prompt_path("agent_openai.txt"))
        for key in overlay:
            with self.subTest(key):
                self.assertNotIn("tool_call block", sections[key])
                self.assertNotIn("<tool_call>", sections[key])
        self.assertIn(openai_format.INSTRUCTION, sections["SYSTEM"])
        self.assertNotIn("{format}", sections["SYSTEM"])

    def test_openai_system_prompt_is_much_shorter(self) -> None:
        tags = len(load_prompt_sections()["SYSTEM"])
        openai = len(load_prompt_sections(TOOL_FORMAT_OPENAI)["SYSTEM"])
        self.assertLess(openai, tags // 3)

    def test_packaged_copy_matches(self) -> None:
        self.assertEqual(
            (REPO / "prompts" / "agent_openai.txt").read_text(encoding="utf-8"),
            (REPO / "src" / "critique_bot" / "prompts" / "agent_openai.txt").read_text(encoding="utf-8"),
        )

    def test_setting_names(self) -> None:
        self.assertEqual(tool_format_of({"tool_format": "OpenAI"}), TOOL_FORMAT_OPENAI)
        self.assertEqual(tool_format_of({"tool_format": "responses"}), TOOL_FORMAT_OPENAI)
        self.assertEqual(tool_format_of({}), "tags")
        self.assertEqual(tool_format_of({"tool_format": "nonsense"}), "tags")
        self.assertEqual(tool_format_of({}, agent.DEFAULT_TOOL_FORMAT), TOOL_FORMAT_OPENAI)
        self.assertEqual(tool_format_of({"tool_format": "tags"}, agent.DEFAULT_TOOL_FORMAT), "tags")


class ToolSetTests(unittest.TestCase):
    def test_minimal_set_is_the_shell_and_file_tools(self) -> None:
        setup = _tool_setup({"tool_format": "openai", "tool_set": "minimal"}, helpers=2, web=False)
        self.assertEqual(set(setup.registry.names()), set(MINIMAL_TOOLS))
        names = {tool["name"] for tool in json.loads(setup.tools_block.split("\n", 1)[1])["tools"]}
        self.assertEqual(names, set(MINIMAL_TOOLS))
        self.assertNotIn("search_code", names)

    def test_full_set_leaves_out_tools_that_are_off(self) -> None:
        setup = _tool_setup({"tool_format": "openai"}, helpers=0, web=False)
        self.assertIsNone(setup.registry)
        names = {tool["name"] for tool in json.loads(setup.tools_block.split("\n", 1)[1])["tools"]}
        self.assertIn("search_code", names)
        self.assertNotIn("delegate", names)
        self.assertNotIn("web_fetch", names)

    def test_tag_format_sends_no_tools_block(self) -> None:
        self.assertEqual(_tool_setup({"tool_format": "tags"}, helpers=2, web=True).tools_block, "")

    def test_crit_sessions_default_to_the_openai_format(self) -> None:
        setup = _tool_setup({}, helpers=2, web=True)
        self.assertEqual(setup.tool_format, TOOL_FORMAT_OPENAI)
        self.assertTrue(setup.tools_block.startswith("TOOLS (OpenAI Responses API)"))


class _Scripted:
    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = {"complete": True}
        self._replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self._replies.pop(0)


class LoopTests(unittest.TestCase):
    def _run(self, root: Path, replies: list[str], task: str = "add a line to notes.txt") -> tuple[_Scripted, list[str]]:
        session = _Scripted(replies)
        outcome: list[str] = []
        with patch("critique_bot.agent._approve_prompt", side_effect=AssertionError("asked")):
            run_agent_loop(
                session,
                workspace=root,
                index_path=None,
                cache_dir=None,
                first_task=task,
                max_rounds=None,
                max_result_chars=8000,
                read_message=lambda: None,
                emit=lambda _text: None,
                approve_mode="auto",
                settings={"tool_format": "openai"},
                outcome=outcome,
            )
        return session, outcome

    def test_a_task_runs_end_to_end_in_the_responses_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("one\n", encoding="utf-8")
            session, outcome = self._run(root, [
                _reply(_message("Reading the file."), _call_item("read_files", "r1", paths=["notes.txt"])),
                _reply(_call_item("edit_file", "e1", path="notes.txt", old_string="one\n", new_string="one\ntwo\n")),
                _reply(_message("COMPLETED")),
            ])
            self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "one\ntwo\n")
            self.assertEqual(outcome, ["COMPLETED"])
            self.assertIn("Reply in the Responses format", session.sent[0])
            results = json.loads(session.sent[1].split("\n\nSTATE", 1)[0])
            self.assertEqual(results["input"][0]["call_id"], "r1")
            self.assertTrue(results["input"][0]["output"]["ok"])
            self.assertIn('"call_id": "e1"', session.sent[2])

    def test_an_empty_output_after_results_does_not_end_the_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("one\n", encoding="utf-8")
            session, outcome = self._run(root, [
                _reply(_call_item("read_files", "r1", paths=["notes.txt"])),
                '{"output":[]}',
                _reply(_call_item("edit_file", "e1", path="notes.txt", old_string="one\n", new_string="one\ntwo\n")),
                _reply(_message("COMPLETED")),
            ])
            self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "one\ntwo\n")
            self.assertEqual(outcome, ["COMPLETED"])
            self.assertEqual(len(session.sent), 4)

    def test_a_reply_without_the_format_still_works(self) -> None:
        # The format is the request; a model that answers with a tag block is still understood.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session, outcome = self._run(
                root,
                ['<tool_call>\n{"tool": "list_files", "arguments": {"path": "."}}\n</tool_call>', "COMPLETED"],
                task="what files are here?",
            )
            self.assertIn("function_call_output", session.sent[1])


if __name__ == "__main__":
    unittest.main()
