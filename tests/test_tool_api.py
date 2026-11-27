"""The tool layer on its own: specs, schemas, exports, and the registry the engine calls."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from critique_bot import agent_tools, tool_api
from critique_bot.agent import run_agent_loop
from critique_bot.tool_api import BUILTIN_SPECS, ToolRegistry, ToolSpec


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _Scripted:
    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = None
        self._replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self._replies.pop(0)


class SpecTests(unittest.TestCase):
    def test_one_spec_per_tool(self) -> None:
        names = [spec.name for spec in BUILTIN_SPECS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(agent_tools.ALLOWED_TOOLS))

    def test_read_only_flags_match_the_tools(self) -> None:
        flagged = {spec.name for spec in BUILTIN_SPECS if spec.read_only}
        self.assertEqual(flagged, set(agent_tools.READ_ONLY))
        self.assertFalse(flagged & set(agent_tools.MUTATING))

    def test_schemas_are_objects_with_known_required_fields(self) -> None:
        for spec in BUILTIN_SPECS:
            with self.subTest(spec.name):
                schema = spec.parameters
                self.assertEqual(schema["type"], "object")
                self.assertTrue(set(schema.get("required", [])) <= set(schema["properties"]))
                json.dumps(schema)  # plain JSON, so any API can take it
                self.assertTrue(spec.description.strip())

    def test_every_description_follows_the_checklist(self) -> None:
        """Purpose, when not to use it, an argument example, the output shape, and the limits."""
        import re

        for spec in BUILTIN_SPECS:
            with self.subTest(spec.name):
                text = spec.description
                for marker in ("Not for:", "Example:", "Returns:", "Limits:"):
                    self.assertIn(marker, text)
                self.assertGreater(text.index("Not for:"), 20, "the purpose comes first")
                found = re.search(r"Example: (\{.*?\})\. ", text)
                self.assertIsNotNone(found, "the example is a JSON object followed by '. '")
                example = json.loads(found.group(1))
                properties = spec.parameters["properties"]
                self.assertEqual([key for key in example if key not in properties], [], "example uses unknown arguments")
                self.assertTrue(set(spec.parameters.get("required", [])) <= set(example), "example leaves out a required argument")
                limits = text[text.index("Limits:"):]
                self.assertRegex(limits, r"\d|one ", "the limit names a number")

    def test_openai_and_mcp_shapes(self) -> None:
        spec = tool_api.registry().spec("read_files")
        assert spec is not None
        openai = spec.to_openai()
        self.assertEqual(openai["type"], "function")
        self.assertEqual(openai["function"]["name"], "read_files")
        self.assertIs(openai["function"]["parameters"], spec.parameters)
        mcp = spec.to_mcp()
        self.assertEqual(mcp["name"], "read_files")
        self.assertIs(mcp["inputSchema"], spec.parameters)
        self.assertTrue(mcp["annotations"]["readOnlyHint"])

    def test_read_only_export_leaves_out_writers(self) -> None:
        names = {item["function"]["name"] for item in tool_api.registry().openai_tools(read_only=True)}
        self.assertIn("read_files", names)
        self.assertNotIn("write_files", names)
        self.assertNotIn("run_command", names)

    def test_spec_lookup_accepts_aliases(self) -> None:
        self.assertEqual(tool_api.registry().spec("read").name, "read_files")


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

        def run(name: str, arguments: dict[str, Any] | None, ctx: Any) -> dict[str, Any]:
            self.calls.append((name, dict(arguments or {})))
            return {"tool": name, "ok": True, "output": "fake listing"}

        self.fake = ToolRegistry(
            [ToolSpec("list_files", "fake", read_only=True)],
            execute=run,
            permission=agent_tools.permission_for,
        )

    def tearDown(self) -> None:
        tool_api.use_registry(None)

    def test_only_refuses_other_tools(self) -> None:
        limited = self.fake.only({"read_files"})
        result = limited.execute("list_files", {}, None)
        self.assertFalse(result["ok"])
        self.assertEqual(self.calls, [])

    def test_engine_runs_tools_through_the_installed_registry(self) -> None:
        old = tool_api.use_registry(self.fake)
        self.assertIsInstance(old, ToolRegistry)
        with tempfile.TemporaryDirectory() as tmp:
            session = _Scripted([_call("list_files", path="."), "Listed. COMPLETED"])
            with patch("critique_bot.engine.ui._approve_prompt", side_effect=AssertionError("asked")):
                run_agent_loop(
                    session,
                    workspace=Path(tmp),
                    index_path=None,
                    cache_dir=None,
                    first_task="list the files",
                    max_rounds=None,
                    max_result_chars=8000,
                    read_message=lambda: None,
                    emit=lambda _text: None,
                    approve_mode="auto",
                )
        self.assertEqual([name for name, _ in self.calls], ["list_files"])
        self.assertIn("fake listing", session.sent[1])

    def test_use_registry_none_restores_the_builtin_tools(self) -> None:
        tool_api.use_registry(self.fake)
        tool_api.use_registry(None)
        self.assertEqual(set(tool_api.registry().names()), set(agent_tools.ALLOWED_TOOLS))


if __name__ == "__main__":
    unittest.main()
