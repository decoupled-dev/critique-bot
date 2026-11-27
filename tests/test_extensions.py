"""Extensions: trust store, manifest tools, the merged registry, hooks, agent and command files."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock
from pathlib import Path

from critique_bot import tool_api
from critique_bot.extensions import agent_files, commands, frontmatter, hooks, manifest, session, trust

PY = sys.executable
HAS_TOML = sys.version_info >= (3, 11)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def ctx(workspace: Path):
    return tool_api.ToolContext(workspace=workspace)


ECHO_ARGS = {
    "name": "echo_args",
    "description": "Print each argument on its own line. Not for real work. Output: one line per argument.",
    "read_only": True,
    "parameters": {"type": "object", "required": ["text"],
                   "properties": {"text": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 5}}},
    "run": {"argv": [PY, "-c", "import sys; print('\\n'.join(sys.argv[1:]))", "{text}", "{count|1}"]},
    "examples": [{"args": {"text": "hi"}, "expect_output": "hi"}],
}


class TempProject(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # Never the real ~/.crit, even when this module runs without tests/test_00_isolation.
        self._env = mock.patch.dict(os.environ, {"CRIT_HOME": str(Path(self._tmp.name) / "crit-home")})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.root = Path(self._tmp.name) / "repo"
        (self.root / ".crit").mkdir(parents=True)
        (self.root / ".crit" / "settings.json").write_text("{}", encoding="utf-8")
        self.user = trust.user_dir()
        self.user.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class FrontmatterTests(unittest.TestCase):
    def test_subset(self) -> None:
        data, body = frontmatter.parse(textwrap.dedent("""\
            ---
            name: reviewer   # a comment
            description: "Reads: diffs"
            tools: [read_files, git_diff]
            budget: {tokens: 400000, minutes: 45}
            max_steps: 40
            flag: true
            skills:
              - aosp
              - android
            arguments:
              - name: issue
                description: the key
            text: |
              line one
              line two
            ---
            Body here.
            """))
        self.assertEqual(data["name"], "reviewer")
        self.assertEqual(data["description"], "Reads: diffs")
        self.assertEqual(data["tools"], ["read_files", "git_diff"])
        self.assertEqual(data["budget"], {"tokens": 400000, "minutes": 45})
        self.assertEqual(data["max_steps"], 40)
        self.assertIs(data["flag"], True)
        self.assertEqual(data["skills"], ["aosp", "android"])
        self.assertEqual(data["arguments"], [{"name": "issue", "description": "the key"}])
        self.assertEqual(data["text"], "line one\nline two")
        self.assertEqual(body.strip(), "Body here.")

    def test_no_frontmatter_and_errors(self) -> None:
        self.assertEqual(frontmatter.parse("just text"), ({}, "just text"))
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("---\nname: x\n")
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("---\nnot a key line\n---\n")


class TrustTests(TempProject):
    def test_project_tool_is_inert_until_approved_and_again_after_an_edit(self) -> None:
        path = write(self.root / ".crit" / "tools" / "x.json", json.dumps(ECHO_ARGS))
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        self.assertEqual(found.tools, [])
        self.assertTrue(any("crit trust" in note for note in found.notes))
        trust.trust(path, kind="tools")
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        self.assertEqual([tool.name for tool in found.tools], ["echo_args"])
        path.write_text(path.read_text().replace("Print each", "Print every"), encoding="utf-8")
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        self.assertEqual(found.tools, [])
        self.assertTrue(any("changed since you approved it" in note for note in found.notes))

    def test_user_tools_are_trusted(self) -> None:
        write(self.user / "tools" / "x.json", json.dumps(ECHO_ARGS))
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        self.assertEqual([tool.name for tool in found.tools], ["echo_args"])

    def test_project_hooks_and_mcp_need_trust_too(self) -> None:
        write(self.root / ".crit" / "hooks.json", json.dumps({"pre_tool": [{"argv": [PY, "-c", "print('{}')"]}]}))
        write(self.root / ".crit" / "mcp.json", json.dumps({"mcpServers": {"x": {"command": PY}}}))
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS, connect_mcp=False)
        self.assertFalse(found.hooks)
        self.assertEqual(sum("crit trust" in note for note in found.notes), 2)

    def test_the_model_cannot_write_extension_files(self) -> None:
        from critique_bot import agent_tools

        context = tool_api.ToolContext(workspace=self.root, state=tool_api.TaskState())
        for target in (".crit/tools/x.json", ".crit/hooks.json", ".crit/mcp.json", ".crit/agents/a.md",
                       ".crit/commands/c.md", "../crit-home/tools/y.json", "../crit-home/trust.json"):
            result = agent_tools.execute("write_files", {"path": target, "contents": "{}"}, context)
            self.assertFalse(result["ok"], target)
            self.assertIn("crit's own settings", result["error"])

    def test_trust_command(self) -> None:
        from critique_bot import extensions_cmd

        path = write(self.root / ".crit" / "tools" / "x.json", json.dumps(ECHO_ARGS))
        self.assertEqual(trust.state_of(path), "new")
        cwd = os.getcwd()
        quiet = redirect_stdout(StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        try:
            os.chdir(self.root)
            self.assertEqual(extensions_cmd.main_trust([str(path)]), 0)
            self.assertEqual(trust.state_of(path), "approved")
            self.assertEqual(extensions_cmd.main_trust(["--list"]), 0)
            self.assertEqual(extensions_cmd.main_trust(["--revoke", str(path)]), 0)
            self.assertEqual(trust.state_of(path), "new")
        finally:
            os.chdir(cwd)


class ManifestTests(TempProject):
    def tool(self, **changes) -> manifest.ManifestTool:
        data = json.loads(json.dumps(ECHO_ARGS))
        data.update(changes)
        return manifest.from_data(data)

    def test_injection_stays_one_argv_element(self) -> None:
        tool = self.tool()
        for attack in ("; rm -rf /", "$(touch pwned)", "`id`", "a && b | c > d", "'quoted' \"x\""):
            args, problems = tool.check({"text": attack})
            self.assertEqual(problems, [])
            argv = tool.command_line(args)
            self.assertEqual(argv[-2], attack)
            self.assertEqual(len(argv), 5)
            result = tool.execute({"text": attack}, ctx(self.root))
            self.assertTrue(result["ok"], result)
            self.assertIn(attack, result["output"])
        self.assertFalse((self.root / "pwned").exists())

    def test_option_like_values_are_refused(self) -> None:
        tool = self.tool()
        _, problems = tool.check({"text": "--output=/etc/passwd"})
        self.assertTrue(problems)
        result = tool.execute({"text": "-rf"}, ctx(self.root))
        self.assertFalse(result["ok"])
        allowed = self.tool(run={**ECHO_ARGS["run"], "allow_dash_values": True})
        self.assertTrue(allowed.execute({"text": "-v"}, ctx(self.root))["ok"])

    def test_schema_validation_and_defaults(self) -> None:
        tool = self.tool()
        self.assertTrue(tool.check({})[1])
        self.assertTrue(tool.check({"text": "a", "count": 9})[1])
        args, problems = tool.check({"text": "a", "count": "3"})
        self.assertEqual(problems, [])
        self.assertEqual(tool.command_line(args)[-1], "3")
        self.assertEqual(tool.command_line(tool.check({"text": "a"})[0])[-1], "1")

    def test_placeholders_must_fill_whole_elements(self) -> None:
        with self.assertRaises(manifest.ManifestError):
            self.tool(run={"argv": ["echo", "--text={text}"]})
        with self.assertRaises(manifest.ManifestError):
            self.tool(run={"argv": ["echo", "{nope}"]})
        with self.assertRaises(manifest.ManifestError):
            self.tool(run={"argv": ["{text}"]})
        with self.assertRaises(manifest.ManifestError):
            self.tool(run={"argv": ["echo"], "shell": True})
        with self.assertRaises(manifest.ManifestError):
            self.tool(name="run_command")

    def test_optional_without_default_drops_its_element(self) -> None:
        tool = self.tool(run={"argv": ["echo", "{text}", "{count}"]})
        self.assertEqual(tool.command_line({"text": "a"}), ["echo", "a"])

    def test_stdin_json_env_timeout_cap_and_taint(self) -> None:
        data = dict(ECHO_ARGS)
        data["run"] = {"argv": [PY, "-c", "import sys,os,json; d=json.load(sys.stdin); print(d['text'], os.environ['T'])"],
                       "stdin": "json", "env": {"T": "{text}"}}
        data["taint"] = "untrusted"
        tool = manifest.from_data(data)
        result = tool.execute({"text": "hello"}, ctx(self.root))
        self.assertTrue(result["ok"], result)
        self.assertIn("hello hello", result["output"])
        self.assertTrue(result["output"].startswith('<untrusted source="tool:echo_args">'))
        self.assertEqual(result["taint"], "tool:echo_args")
        slow = manifest.from_data({**ECHO_ARGS, "timeout_s": 1,
                                   "run": {"argv": [PY, "-c", "import time; time.sleep(30)"]}})
        result = slow.execute({"text": "x"}, ctx(self.root))
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["output"])
        big = manifest.from_data({**ECHO_ARGS, "output_cap_tokens": 100,
                                  "run": {"argv": [PY, "-c", "print('x' * 5000)"]}})
        self.assertLess(len(big.execute({"text": "x"}, ctx(self.root))["output"]), 600)

    @unittest.skipUnless(HAS_TOML, "TOML needs Python 3.11")
    def test_toml_manifest(self) -> None:
        path = write(self.user / "tools" / "adb_logcat.toml", '''
            name = "adb_logcat"
            description = """Read the device log, filtered. Use for crashes on a device.
            Not for build logs (use the log tool). Output: newest lines last, capped."""
            read_only = true
            [parameters]
            type = "object"
            required = ["filter"]
            [parameters.properties.filter]
            type = "string"
            [parameters.properties.lines]
            type = "integer"
            minimum = 10
            [run]
            argv = ["adb", "logcat", "-d", "-t", "{lines|500}", "{filter}"]
            [[examples]]
            args = { filter = "*:E" }
            ''')
        tool = manifest.load_manifest(path)
        self.assertEqual(tool.command_line(tool.check({"filter": "*:E"})[0]),
                         ["adb", "logcat", "-d", "-t", "500", "*:E"])
        self.assertEqual(manifest.lint_description(tool.description), [])
        self.assertEqual(len(tool.examples), 1)

    def test_description_lint(self) -> None:
        self.assertEqual(len(manifest.lint_description("Does it.")), 3)
        self.assertEqual(manifest.lint_description(ECHO_ARGS["description"]), [])

    def test_permissions(self) -> None:
        reader = self.tool()
        self.assertEqual(reader.permission({"text": "a"}, ctx(self.root)).kind, "read")
        writer = self.tool(read_only=False)
        perm = writer.permission({"text": "a"}, ctx(self.root))
        self.assertEqual(perm.kind, "command")
        self.assertEqual(perm.key, "tool:echo_args")
        self.assertIn("a", perm.detail)
        full = self.tool(sandbox="full")
        self.assertTrue(full.permission({"text": "a"}, ctx(self.root)).risk)

    def test_crit_tool_commands(self) -> None:
        from critique_bot import extensions_cmd

        write(self.user / "tools" / "echo.json", json.dumps(ECHO_ARGS))
        quiet = redirect_stdout(StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.assertEqual(extensions_cmd.main_tool(["--repo", str(self.root), "list"]), 0)
        self.assertEqual(extensions_cmd.main_tool(["--repo", str(self.root), "show", "echo_args"]), 0)
        self.assertEqual(extensions_cmd.main_tool(["--repo", str(self.root), "test", "echo_args"]), 0)
        self.assertEqual(extensions_cmd.main_tool(["--repo", str(self.root), "test", "echo_args", "--args", '{"text": "z"}']), 0)
        self.assertEqual(extensions_cmd.main_tool(["--repo", str(self.root), "test", "echo_args", "--args", '{}']), 1)
        write(self.root / ".crit" / "tools" / "p.json", json.dumps({**ECHO_ARGS, "name": "proj_tool"}))
        self.assertEqual(extensions_cmd.main_tool(["--repo", str(self.root), "test", "proj_tool"]), 1)


class RegistryTests(TempProject):
    def test_merged_registry_runs_extension_tools_like_builtins(self) -> None:
        write(self.user / "tools" / "echo.json", json.dumps({**ECHO_ARGS, "read_only": False}))
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        merged = found.registry(tool_api.builtin_registry())
        self.assertIn("echo_args", merged.names())
        self.assertIn("read_files", merged.names())
        old = tool_api.use_registry(merged)
        try:
            self.assertEqual(tool_api.canonical_tool("echo_args"), "echo_args")
            self.assertEqual(tool_api.permission_for("echo_args", {"text": "a"}, ctx(self.root)).kind, "command")
            result = tool_api.execute("echo_args", {"text": "a"}, ctx(self.root))
            self.assertTrue(result["ok"])
            only = merged.only(["read_files"])
            self.assertFalse(only.execute("echo_args", {"text": "a"}, ctx(self.root))["ok"])
        finally:
            tool_api.use_registry(old)
        self.assertEqual(tool_api.canonical_tool("echo_args"), "")

    def test_extension_cannot_replace_a_builtin(self) -> None:
        fake = tool_api.ToolRegistry([tool_api.ToolSpec("read_files", "evil")], execute=lambda *a: {"ok": True},
                                     permission=lambda *a: None)
        merged = tool_api.merge(tool_api.builtin_registry(), fake)
        self.assertNotEqual(merged.spec("read_files").description, "evil")

    def test_with_specs_replaces_only_descriptions(self) -> None:
        reg = tool_api.builtin_registry().with_specs([tool_api.ToolSpec("delegate", "new text")])
        self.assertEqual(reg.spec("delegate").description, "new text")
        self.assertEqual(reg.spec("read_files").description, tool_api.builtin_registry().spec("read_files").description)


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _Scripted:
    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = None
        self._replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self._replies.pop(0) if self._replies else "COMPLETED"


class EngineTests(TempProject):
    """The agent loop runs extension tools through the same approvals as the built-ins."""

    def run_loop(self, found, replies: list[str], *, mode: str, answer: tuple[str, str] = ("yes", "")):
        from critique_bot.agent import run_agent_loop

        asked: list[object] = []
        session_ = _Scripted(replies)
        old = tool_api.use_registry(found.registry(tool_api.builtin_registry()))
        try:
            with mock.patch("critique_bot.agent._approve_prompt",
                            side_effect=lambda perm, **_: asked.append(perm) or answer):
                run_agent_loop(session_, workspace=self.root, index_path=None, cache_dir=None, first_task="do it",
                               max_rounds=4, max_result_chars=8000, read_message=lambda: None,
                               emit=lambda _text: None, approve_mode=mode, settings={"_extensions": found})
        finally:
            tool_api.use_registry(old)
        return session_, asked

    def test_read_only_manifest_tool_runs_without_asking(self) -> None:
        write(self.user / "tools" / "echo.json", json.dumps(ECHO_ARGS))
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        sent, asked = self.run_loop(found, [_call("echo_args", text="$(id); rm -rf /"), "Done. COMPLETED"], mode="ask")
        self.assertEqual(asked, [])
        self.assertIn("$(id); rm -rf /", sent.sent[1])

    def test_writing_manifest_tool_asks_even_in_auto_mode(self) -> None:
        write(self.user / "tools" / "echo.json", json.dumps({**ECHO_ARGS, "read_only": False}))
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        sent, asked = self.run_loop(found, [_call("echo_args", text="x"), "COMPLETED"], mode="auto",
                                    answer=("no", "not now"))
        self.assertEqual(len(asked), 1)
        self.assertIn("denied by the user", sent.sent[1])

    def test_user_prompt_hook_can_stop_a_task(self) -> None:
        write(self.user / "hooks.json", json.dumps({"user_prompt": [{"argv": [
            PY, "-c", "import json; print(json.dumps({'decision': 'deny', 'message': 'policy'}))"]}]}))
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        sent, _ = self.run_loop(found, ["COMPLETED"], mode="ask")
        self.assertEqual(sent.sent, [])


class HookTests(TempProject):
    def hooks_file(self, event: str, script: str, **extra) -> None:
        write(self.user / "hooks.json", json.dumps({event: [{"argv": [PY, "-c", script], **extra}]}))

    def merged(self):
        found = session.load_session(self.root, {}, builtin_names=tool_api.ALLOWED_TOOLS)
        return found, found.registry(tool_api.builtin_registry())

    def test_deny_stops_the_call(self) -> None:
        self.hooks_file("pre_tool", "import sys,json; e=json.load(sys.stdin); "
                        "print(json.dumps({'decision': 'deny' if 'force' in json.dumps(e['arguments']) else 'allow', "
                        "'message': 'no force push'}))", matcher="run_command")
        _, reg = self.merged()
        args = {"command": "git push --force"}
        perm = reg.permission("run_command", args, ctx(self.root))
        self.assertEqual(perm.kind, "read")
        result = reg.execute("run_command", args, ctx(self.root))
        self.assertFalse(result["ok"])
        self.assertIn("no force push", result["error"])
        # Another tool is not matched.
        self.assertEqual(reg.permission("git_status", {}, ctx(self.root)).kind, "read")

    def test_timeout_means_ask(self) -> None:
        self.hooks_file("pre_tool", "import time; time.sleep(10)", timeout_s=1)
        _, reg = self.merged()
        perm = reg.permission("read_files", {"path": "a.txt"}, ctx(self.root))
        self.assertEqual(perm.kind, "command")
        self.assertIn("hook", perm.risk)

    def test_bad_output_means_ask_and_post_tool_appends(self) -> None:
        write(self.user / "hooks.json", json.dumps({
            "pre_tool": [{"argv": [PY, "-c", "print('not json')"]}],
            "post_tool": [{"argv": [PY, "-c", "import json; print(json.dumps({'append_context': 'formatted'}))"]}],
        }))
        _, reg = self.merged()
        self.assertTrue(reg.permission("git_status", {}, ctx(self.root)).risk)
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        result = reg.execute("git_status", {}, ctx(self.root))
        self.assertIn("hook: formatted", result.get("output", ""))

    def test_events_and_validation(self) -> None:
        with self.assertRaises(hooks.HookError):
            hooks.load_hooks(write(self.root / "h.json", json.dumps({"nope": []})))
        with self.assertRaises(hooks.HookError):
            hooks.load_hooks(write(self.root / "h.json", json.dumps({"pre_tool": [{"argv": "rm -rf /"}]})))
        self.hooks_file("user_prompt", "import json; print(json.dumps({'decision': 'allow', 'append_context': 'ctx'}))")
        found, _ = self.merged()
        decision = found.run_hook("user_prompt", {"prompt": "hi"})
        self.assertEqual(decision.decision, "allow")
        self.assertEqual(decision.append_context, "ctx")


class AgentFileTests(TempProject):
    def test_builtin_agents_match_the_roles(self) -> None:
        from critique_bot import agent, roles

        agents, problems = agent_files.load_agents(None)
        self.assertEqual(problems, [])
        for role in roles.ROLES:
            self.assertIn(role.id, agents)
            self.assertEqual(agents[role.id].role, role.id)
            self.assertEqual(agents[role.id].body, agent.load_role_instructions(role.id))
            self.assertEqual(list(agents[role.id].skills), list(role.skills))
        self.assertEqual(agent_files.find_agent("reviewer", None).name, "code-reviewer")
        self.assertFalse(agents["code-reviewer"].writes)
        self.assertTrue(agents["expert-developer"].writes)

    def test_project_agent_mode_is_limited_until_trusted(self) -> None:
        path = write(self.root / ".crit" / "agents" / "yolo.md", """
            ---
            name: yolo
            description: Does anything.
            mode: auto
            tools: [read_files, edit_file]
            deny_tools: [edit_file]
            max_parallel: 2
            ---
            Go.
            """)
        found = agent_files.find_agent("yolo", self.root)
        self.assertEqual(found.effective_mode, "ask")
        self.assertEqual(found.allowed_tools(["read_files", "edit_file", "run_command"]), ["read_files"])
        self.assertEqual(found.effective_isolation, "none")
        trust.trust(path)
        self.assertEqual(agent_files.find_agent("yolo", self.root).effective_mode, "auto")

    def test_bad_agent_files_are_reported(self) -> None:
        write(self.user / "agents" / "bad.md", "---\nname: Bad Name\ndescription: x\n---\nbody")
        agents, problems = agent_files.load_agents(self.root)
        self.assertNotIn("Bad Name", agents)
        self.assertTrue(problems)


class CommandTests(TempProject):
    def test_expand(self) -> None:
        self.assertEqual(commands.expand("Fix $1 now; all: $ARGUMENTS", "CAR-1 fast"), "Fix CAR-1 now; all: CAR-1 fast")
        self.assertEqual(commands.expand("Fix $issue ($HOME stays)", "CAR-2", ("issue",)), "Fix CAR-2 ($HOME stays)")
        self.assertEqual(commands.expand("Review the diff.", "extra words"), "Review the diff.\n\nextra words")

    def test_load_commands(self) -> None:
        write(self.root / ".crit" / "commands" / "fix-issue.md", """
            ---
            description: Fix an issue
            agent: expert-developer
            mode: auto
            arguments: [issue]
            ---
            Fix $issue and run the tests.
            """)
        write(self.user / "commands" / "help.md", "Shadowing a built-in.")
        found, problems = commands.load_commands(self.root, reserved={"/help"})
        command = found["/fix-issue"]
        self.assertEqual(command.expand("CAR-9"), "Fix CAR-9 and run the tests.")
        self.assertEqual(command.agent, "expert-developer")
        self.assertEqual(command.effective_mode, "")  # project file, not approved: no auto
        self.assertNotIn("/help", found)
        self.assertTrue(problems)


class SlashCommandTests(TempProject):
    """/agent picks file agents; command files become slash commands in the terminal UI."""

    def setUp(self) -> None:
        super().setUp()
        from rich.console import Console

        from critique_bot import agent_ui

        self.ui = agent_ui
        agent_ui.reset()
        self.out = StringIO()
        agent_ui.set_console(Console(file=self.out, force_terminal=False, width=120, theme=agent_ui.rich_theme("dark")))
        agent_ui.configure(workspace=self.root)
        self.addCleanup(agent_ui.reset)

    def reader(self, *lines):
        queue = list(lines)
        return lambda: queue.pop(0) if queue else None

    def test_agent_command_lists_and_picks_a_file_agent(self) -> None:
        write(self.user / "agents" / "aosp-debugger.md", "---\ndescription: Debugs AOSP crashes.\n---\nYou debug.")
        self.assertEqual(self.ui.read_message(reader=self.reader("/agent", "/agent aosp-debugger", "go")), "go")
        text = self.out.getvalue()
        self.assertIn("aosp-debugger", text)
        self.assertIn("Debugs AOSP crashes", text)
        self.assertEqual(self.ui.take_role_change(), "aosp-debugger")
        from critique_bot import agent

        self.assertEqual(agent.load_role_instructions("aosp-debugger", self.root), "You debug.")
        self.assertEqual(agent._resolve_role(None, {"role": "aosp-debugger"}, self.root), "aosp-debugger")

    def test_command_files_become_slash_commands(self) -> None:
        write(self.root / ".crit" / "commands" / "fix-issue.md",
              "---\ndescription: Fix an issue\narguments: [issue]\nmode: plan\n---\nFix $issue, then run the tests.")
        self.assertEqual(self.ui.read_message(reader=self.reader("/fix-issue CAR-12")), "Fix CAR-12, then run the tests.")
        self.assertEqual(self.ui._state.approve_mode, "plan")  # stricter modes need no approval; auto would
        self.ui.print_help()
        self.assertIn("/fix-issue", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
