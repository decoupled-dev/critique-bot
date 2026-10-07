from __future__ import annotations

import io
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from critique_bot import agent_ui, log


@dataclass
class _Permission:
    kind: str
    summary: str
    key: str
    detail: str = ""


_DIFF = """--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,3 @@
 def greet():
-    print("Hello, World!")
+    print("Hello, crit!")
     return 1
"""


def _console(*, terminal: bool, width: int = 100) -> Console:
    return Console(
        file=io.StringIO(),
        force_terminal=terminal,
        width=width,
        record=True,
        color_system="truecolor" if terminal else None,
        theme=agent_ui.rich_theme("dark"),
        highlight=False,
        markup=False,
        emoji=False,
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        agent_ui.reset()
        self.con = _console(terminal=False)
        agent_ui.set_console(self.con)

    def tearDown(self) -> None:
        agent_ui.reset()

    def text(self) -> str:
        return self.con.export_text(clear=True)


class ToolRenderingTests(_Base):
    def test_titles_follow_claude_code_names(self) -> None:
        self.assertEqual(agent_ui.tool_title("read_files"), "Read")
        self.assertEqual(agent_ui.tool_title("edit_file"), "Update")
        self.assertEqual(agent_ui.tool_title("find_files"), "Glob")
        self.assertEqual(agent_ui.tool_title("todo"), "Update Todos")
        self.assertEqual(agent_ui.tool_title("web_fetch"), "Fetch")
        self.assertEqual(agent_ui.tool_title("run_command", {"shell": "pwsh"}), "PowerShell")
        self.assertEqual(agent_ui.tool_title("run_command", {"shell": "bash"}), "Bash")
        agent_ui.configure(shell=type("S", (), {"kind": "powershell", "label": "PowerShell 7"})())
        self.assertEqual(agent_ui.tool_title("run_command", {}), "PowerShell")

    def test_shell_switch_is_queued_for_the_model(self) -> None:
        from critique_bot import agent_shell

        pwsh = agent_shell.Shell("pwsh", "/usr/bin/pwsh", "pwsh")
        session = type("Session", (), {"shell": None})()
        agent_ui.configure(session_shell=session)
        with patch.object(agent_shell, "available_shells", lambda: {"pwsh": pwsh}):
            agent_ui._shell_command("pwsh")
        self.assertIs(session.shell, pwsh)
        self.assertEqual(agent_ui.state().shell_label, pwsh.label)
        self.assertIs(agent_ui.take_shell_change(), pwsh)
        self.assertIsNone(agent_ui.take_shell_change())
        agent_ui.state().session_shell = None

    def test_read_line_and_summary(self) -> None:
        agent_ui.tool_done(
            "read_files",
            {"path": "src/a.py"},
            {"tool": "read_files", "ok": True, "output": "...", "ui": {"summary": "Read 120 lines"}},
        )
        self.assertEqual(self.text(), "● Read(src/a.py)\n  ⎿  Read 120 lines\n")

    def test_summary_falls_back_to_first_output_line(self) -> None:
        agent_ui.tool_done("list_files", {"path": "."}, {"ok": True, "output": "\nsrc/\nREADME.md"})
        self.assertEqual(self.text(), "● List(.)\n  ⎿  src/\n")

    def test_edit_shows_numbered_diff(self) -> None:
        agent_ui.tool_done(
            "edit_file",
            {"path": "src/app.py"},
            {"ok": True, "output": "", "ui": {"summary": "Updated src/app.py (+1 -1)", "diff": _DIFF}},
        )
        self.assertEqual(
            self.text(),
            "● Update(src/app.py)\n"
            "  ⎿  Updated src/app.py (+1 -1)\n"
            "      10   def greet():\n"
            '      11 -     print("Hello, World!")\n'
            '      11 +     print("Hello, crit!")\n'
            "      12       return 1\n",
        )

    def test_long_diff_is_cut_with_a_count(self) -> None:
        body = "".join(f"+line {n}\n" for n in range(45))
        rows = agent_ui.render_diff("@@ -0,0 +1,45 @@\n" + body)
        self.assertEqual(len(rows), 31)
        self.assertEqual(rows[-1].plain.strip(), "… +15 lines")

    def test_diff_colors_on_a_terminal(self) -> None:
        con = _console(terminal=True)
        agent_ui.set_console(con)
        agent_ui.tool_done("edit_file", {"path": "a.py"}, {"ok": True, "ui": {"summary": "Updated", "diff": _DIFF}})
        out = con.file.getvalue()
        self.assertIn("\x1b[", out)
        self.assertIn("48;2;92;36;48", out)  # removed-line background from the dark theme

    def test_command_shows_exit_and_five_lines(self) -> None:
        output = "\n".join(f"line {n}" for n in range(9))
        agent_ui.tool_done(
            "run_command",
            {"command": "npm test", "shell": "bash"},
            {"ok": True, "output": output, "ui": {"summary": "exit 0 · 2.1s"}},
        )
        text = self.text()
        self.assertTrue(text.startswith("● Bash(npm test)\n  ⎿  exit 0 · 2.1s\n     line 0\n"))
        self.assertIn("     line 4\n", text)
        self.assertNotIn("line 5\n", text)
        self.assertIn("… +4 lines", text)

    def test_error_is_red_and_friendly(self) -> None:
        agent_ui.tool_done(
            "edit_file",
            {"path": "a.py"},
            {"ok": False, "error": "old_string not found in a.py"},
            friendly="Couldn't find the exact text to change.",
        )
        self.assertEqual(
            self.text(),
            "● Update(a.py)\n"
            "  ⎿  Error: Couldn't find the exact text to change.\n"
            "     old_string not found in a.py\n",
        )

    def test_todos_render_with_boxes(self) -> None:
        todos = [
            {"content": "Read the config", "status": "completed"},
            {"content": "Fix the parser", "status": "in_progress"},
            {"content": "Run tests", "status": "pending"},
        ]
        agent_ui.tool_done("todo", {"todos": todos}, {"ok": True, "output": "ok"})
        self.assertEqual(
            self.text(),
            "● Update Todos\n"
            "  ⎿  ☒ Read the config\n"
            "     ◐ Fix the parser\n"
            "     ☐ Run tests\n",
        )

    def test_search_summary(self) -> None:
        self.assertEqual(
            agent_ui.arg_summary("search_code", {"pattern": "def main", "path": "src"}),
            'pattern: "def main", path: "src"',
        )


class FinalDiffTests(_Base):
    def test_end_of_task_diff_keeps_one_row_per_line(self) -> None:
        from critique_bot import agent

        self.assertTrue(agent._show_diff_in_ui(_DIFF))
        rows = [line for line in self.text().splitlines() if line.strip()]
        self.assertIn('      11 -     print("Hello, World!")', rows)
        self.assertIn('      11 +     print("Hello, crit!")', rows)


class LiveRegionTests(_Base):
    def test_non_tty_prints_no_spinner_and_no_ansi(self) -> None:
        agent_ui.waiting(True)
        agent_ui.tool_start("run_command", {"command": "make"})
        agent_ui.tool_output("compiling\n")
        agent_ui.waiting(False)
        self.assertIsNone(agent_ui._live)
        agent_ui.tool_done("run_command", {"command": "make"}, {"ok": True, "output": "", "ui": {"summary": "exit 0"}})
        text = self.con.file.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("Thinking", text)
        self.assertEqual(text, "● Bash(make)\n  ⎿  exit 0\n")

    def test_live_view_shows_tail_and_spinner(self) -> None:
        agent_ui.tool_start("run_command", {"command": "make"})
        agent_ui.tool_output("a\nb\nc\nd\ne\nf\ng")
        agent_ui.waiting(True, "Fetching")
        state = agent_ui.state()
        con = _console(terminal=False)
        con.print(agent_ui.live_renderable(now=state.waiting_since + 23))
        text = con.export_text()
        self.assertIn("● Bash(make)", text)
        self.assertNotIn("  b\n", text)
        self.assertIn("     g\n", text)
        self.assertIn("Fetching… (23s · ctrl+c to interrupt)", text)
        agent_ui.waiting(False)
        agent_ui.tool_done("run_command", {"command": "make"}, {"ok": True, "ui": {"summary": "exit 0"}})
        self.assertIsNone(agent_ui.state().tool_title)

    def test_thinking_rotates_verbs(self) -> None:
        labels = set()
        for _ in range(3):
            agent_ui.waiting(True)
            labels.add(agent_ui.state().waiting_label)
            agent_ui.waiting(False)
        self.assertEqual(len(labels), 3)

    def test_terminal_live_starts_and_stops(self) -> None:
        con = _console(terminal=True)
        agent_ui.set_console(con)
        agent_ui.waiting(True)
        self.assertIsNotNone(agent_ui._live)
        agent_ui.waiting(False)
        self.assertIsNone(agent_ui._live)

    def test_log_loading_is_routed_through_the_bridge(self) -> None:
        con = _console(terminal=True)
        agent_ui.set_console(con)
        agent_ui.install_log_bridge()
        try:
            with patch.object(log.sys.stderr, "isatty", return_value=True, create=True):
                with log.loading("Thinking..."):
                    self.assertGreater(agent_ui.state().waiting_depth, 0)
                    self.assertIsNotNone(agent_ui._live)
                    with log.loading("Waiting for sign-in..."):
                        self.assertEqual(agent_ui.state().waiting_label, "Waiting for sign-in")
            self.assertEqual(agent_ui.state().waiting_depth, 0)
            self.assertIsNone(agent_ui._live)
        finally:
            agent_ui.shutdown()
        self.assertIsNone(log._active_spinner)


class TextTests(_Base):
    def test_notes_and_status(self) -> None:
        agent_ui.note("task", "Working on: fix it")
        agent_ui.note("bad", "The check failed.")
        agent_ui.final_status("FAILED", ok=False)
        agent_ui.final_status("COMPLETED", ok=True)
        agent_ui.final_status("INTERRUPTED", ok=False)
        self.assertEqual(
            self.text(),
            "> Working on: fix it\n  The check failed.\n\n✗ Failed: The check failed.\n\n✓ Done\n\n⏹ Interrupted\n",
        )

    def test_assistant_plain_when_piped(self) -> None:
        agent_ui.assistant("# Title\n\n`code` and **bold**")
        self.assertEqual(self.text(), "# Title\n\n`code` and **bold**\n\n")

    def test_assistant_markdown_on_a_terminal(self) -> None:
        con = _console(terminal=True)
        agent_ui.set_console(con)
        agent_ui.assistant("Some **bold** text\n\n```python\nx = 1\n```")
        text = con.export_text()
        self.assertIn("● Some bold text", text)
        self.assertIn("x = 1", text)
        self.assertNotIn("**", text)

    def test_header_plain_when_piped(self) -> None:
        agent_ui.configure(workspace=Path("/w"), model="ChatGPT 5.5", approve_mode="auto")
        agent_ui.welcome_header(version="1.2")
        self.assertEqual(self.text(), f"crit v1.2 · {Path('/w')} · model: ChatGPT 5.5 · permissions: auto\n")

    def test_header_box_on_a_terminal(self) -> None:
        con = _console(terminal=True)
        agent_ui.set_console(con)
        agent_ui.configure(workspace=Path("/w"), model="ChatGPT 5.5")
        agent_ui.welcome_header(version="1.2")
        text = con.export_text()
        self.assertIn("✻ Welcome to crit v1.2", text)
        self.assertIn("/help for commands", text)
        self.assertIn("model: ChatGPT 5.5", text)
        self.assertIn("╭", text)

    def test_no_color_keeps_layout_without_escape_colors(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            con = Console(file=io.StringIO(), force_terminal=True, width=80, theme=agent_ui.rich_theme("dark"))
            agent_ui.set_console(con)
            agent_ui.tool_done("edit_file", {"path": "a.py"}, {"ok": True, "ui": {"summary": "Updated", "diff": _DIFF}})
            out = con.file.getvalue()
        self.assertIn("(a.py)", out)  # bold may stay; NO_COLOR only drops colors
        self.assertNotIn("38;2", out)
        self.assertNotIn("48;2", out)

    def test_every_theme_maps(self) -> None:
        for theme_id in ("auto", "dark", "light", "dark-colorblind", "light-colorblind", "dark-ansi", "light-ansi"):
            theme = agent_ui.rich_theme(theme_id)
            self.assertIn("crit.add", theme.styles)
        self.assertEqual(agent_ui.theme_id_for("nonsense"), "dark")

    def test_ascii_glyphs_on_a_narrow_codepage(self) -> None:
        con = Console(file=io.StringIO(), force_terminal=False, width=80)
        con._encoding = "cp1252"  # type: ignore[attr-defined]
        agent_ui.set_console(con)
        with patch.object(type(con), "encoding", property(lambda self: "cp1252")):
            self.assertIs(agent_ui.glyphs(), agent_ui.ASCII_GLYPHS)


class InputTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "node_modules" / "pkg" / "app.js").write_text("", encoding="utf-8")
        (self.root / "README.md").write_text("", encoding="utf-8")
        agent_ui.configure(workspace=self.root, cache_dir=self.root / ".bot" / "cache")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def _reader(self, *lines: str | None):
        items = iter(lines)
        return lambda: next(items)

    def test_slash_commands_are_handled_in_the_ui(self) -> None:
        reader = self._reader("/help", "/permissions", "/status", "/tools", "fix it")
        self.assertEqual(agent_ui.read_message(reader=reader), "fix it")
        text = self.text()
        self.assertIn("/undo", text)
        self.assertIn("Enter", text)
        self.assertIn("permissions  auto", text)
        self.assertEqual(agent_ui.approve_mode(), "auto")
        self.assertIn("Update", text)

    def test_new_and_exit_tokens(self) -> None:
        self.assertEqual(agent_ui.read_message(reader=self._reader("/new")), agent_ui.NEW_CHAT)
        self.assertIsNone(agent_ui.read_message(reader=self._reader("/exit")))
        self.assertIsNone(agent_ui.read_message(reader=self._reader("quit")))
        self.assertIsNone(agent_ui.read_message(reader=self._reader(None)))

    def test_unknown_command_is_not_sent_but_paths_are(self) -> None:
        reader = self._reader("/frobnicate", "/usr/bin/env is broken")
        self.assertEqual(agent_ui.read_message(reader=reader), "/usr/bin/env is broken")
        self.assertIn("Unknown command /frobnicate", self.text())

    def test_mentions_become_a_referenced_files_note(self) -> None:
        message = agent_ui.read_message(reader=self._reader("look at @src/app.py and @nope.txt, thanks"))
        self.assertEqual(message, "look at @src/app.py and @nope.txt, thanks\n\nReferenced files: src/app.py")

    def test_undo_command_restores(self) -> None:
        with patch("critique_bot.agent_edit.undo_last", return_value=["src/app.py"]) as undo:
            agent_ui.read_message(reader=self._reader("/undo", "/exit"))
        undo.assert_called_once()
        self.assertIn("Restored src/app.py", self.text())

    def test_file_index_skips_build_dirs_and_ranks_basenames(self) -> None:
        index = agent_ui._FileIndex(self.root)
        paths = index.paths()
        self.assertIn("src/app.py", paths)
        self.assertFalse(any(path.startswith("node_modules") for path in paths))
        self.assertEqual(index.match("app")[0], "src/app.py")
        self.assertIn("README.md", index.match("rdme"))

    def test_completer_offers_commands_and_paths(self) -> None:
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        completer = agent_ui._make_completer()
        names = [c.text for c in completer.get_completions(Document("/un"), CompleteEvent())]
        self.assertEqual(names, ["/undo"])
        paths = [c.text for c in completer.get_completions(Document("fix @app"), CompleteEvent())]
        self.assertEqual(paths[0], "src/app.py")
        self.assertEqual(list(completer.get_completions(Document("plain words"), CompleteEvent())), [])

    def test_non_tty_read_returns_none(self) -> None:
        self.assertIsNone(agent_ui.read_message())


class PromptToolkitTests(_Base):
    def _run(self, keys: str, func, **kwargs):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return func(pt_input=pipe, pt_output=DummyOutput(), **kwargs)

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        agent_ui.configure(workspace=Path(self.tmp.name), history_path=Path(self.tmp.name) / ".bot" / "history")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def test_enter_submits(self) -> None:
        self.assertEqual(self._run("hello\r", agent_ui.prompt_line), "hello")
        self.assertIn("hello", (Path(self.tmp.name) / ".bot" / "history").read_text(encoding="utf-8"))

    def test_trailing_backslash_and_ctrl_j_add_lines(self) -> None:
        self.assertEqual(self._run("one\\\rtwo\nthree\r", agent_ui.prompt_line), "one\ntwo\nthree")

    def test_alt_enter_adds_a_line(self) -> None:
        self.assertEqual(self._run("a\x1b\rb\r", agent_ui.prompt_line), "a\nb")

    def test_ctrl_c_clears_then_twice_exits(self) -> None:
        self.assertIsNone(self._run("typed\x03\x03\x03", agent_ui.prompt_line))

    def test_ctrl_c_on_text_only_clears(self) -> None:
        self.assertEqual(self._run("typed\x03next\r", agent_ui.prompt_line), "next")

    def test_ctrl_d_exits(self) -> None:
        self.assertIsNone(self._run("\x04", agent_ui.prompt_line))

    def test_bracketed_paste_stays_one_message(self) -> None:
        pasted = "\x1b[200~line one\nline two\x1b[201~\r"
        self.assertEqual(self._run(pasted, agent_ui.prompt_line), "line one\nline two")

    def test_approval_yes_always_no(self) -> None:
        permission = _Permission("command", "Run: npm test", "command:npm", "npm test")
        self.assertEqual(self._run("1", agent_ui.approve, permission=permission), ("yes", ""))
        self.assertEqual(self._run("a", agent_ui.approve, permission=permission), ("always", ""))
        self.assertEqual(self._run("\x1b[B\r", agent_ui.approve, permission=permission), ("always", ""))
        self.assertEqual(self._run("n\r", agent_ui.approve, permission=permission)[0], "no")
        text = self.text()
        self.assertIn("Bash command", text)
        self.assertIn("Run: npm test", text)
        self.assertIn("Do you want to proceed?", text)

    def test_no_with_a_reason(self) -> None:
        permission = _Permission("edit", "Edit src/a.py", "edit", _DIFF)
        answer = self._run("3use the helper instead\r", agent_ui.approve, permission=permission)
        self.assertEqual(answer, ("no", "use the helper instead"))
        self.assertIn('print("Hello, crit!")', self.text())

    def test_escape_declines(self) -> None:
        permission = _Permission("network", "Fetch https://example.com", "network:example.com")
        self.assertEqual(self._run("\x1b", agent_ui.approve, permission=permission), ("no", ""))

    def test_outside_never_offers_always(self) -> None:
        permission = _Permission("outside", "Read /etc/hosts", "outside")
        options = [answer for answer, _label in agent_ui.approval_options(permission)]
        self.assertEqual(options, ["yes", "no"])
        self.assertEqual(self._run("2\r", agent_ui.approve, permission=permission)[0], "no")
        labels = agent_ui.approval_options(_Permission("command", "Run: npm i", "command:npm"))
        self.assertIn("npm commands", labels[1][1])

    def test_non_tty_approval_and_auto(self) -> None:
        permission = _Permission("command", "Run: rm x", "command:rm")
        self.assertEqual(agent_ui.approve(permission), ("no", "no terminal to approve"))
        agent_ui.configure(approve_mode="auto")
        self.assertEqual(agent_ui.approve(permission), ("yes", ""))


class _Provider:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def session(self):
        return self


class RunAgentTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        with patch("critique_bot.bot_home.rebuild_index") as rebuild:
            from critique_bot.code_index import IndexStats

            rebuild.return_value = IndexStats(files=0, symbols=0)
            from critique_bot.bot_home import init_bot_home

            self.home = init_bot_home(self.root)
        from critique_bot.config import BotConfig, Selectors

        self.config = BotConfig(
            url="https://chatgpt.com/",
            selectors=Selectors(prompt_input="textarea", assistant_messages=".markdown"),
            model="ChatGPT 5.5",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def _run(self, loop, **kwargs):
        from critique_bot import agent

        with patch("critique_bot.provider.open_provider", return_value=_Provider()), patch.object(
            agent, "run_agent_loop", loop
        ), patch.object(agent, "_ensure_code_graph"), patch.object(agent, "_open_shell_session", return_value=None):
            return agent.run_agent(
                self.config, self.home, "do it", max_rounds=None, output_dir=None, headed=False, **kwargs
            )

    def test_ctrl_c_still_saves_the_transcript(self) -> None:
        seen = {}

        def loop(session, *, turns=None, approve_mode="ask", **kwargs):
            seen["approve_mode"] = approve_mode
            turns.append({"role": "user", "content": "do it"})
            turns.append({"role": "assistant", "content": "working"})
            raise KeyboardInterrupt

        self.assertEqual(self._run(loop, approve_mode="auto"), 130)
        self.assertEqual(seen["approve_mode"], "auto")
        saved = list(self.home.sessions_dir.glob("*/agent.md"))
        self.assertEqual(len(saved), 1)
        self.assertIn("working", saved[0].read_text(encoding="utf-8"))
        self.assertIn("⏹ Interrupted", self.text())

    def test_settings_permissions_auto(self) -> None:
        from critique_bot.bot_home import update_settings

        update_settings(self.home, permissions="auto")
        seen = {}

        def loop(session, *, approve_mode="ask", outcome=None, **kwargs):
            seen["approve_mode"] = approve_mode
            return []

        self.assertEqual(self._run(loop), 0)
        self.assertEqual(seen["approve_mode"], "auto")

    def test_old_loop_signature_still_works(self) -> None:
        def loop(session, *, workspace, index_path, cache_dir, first_task, max_rounds, max_result_chars,
                 seed=None, outcome=None, check_command=None, shell=None):
            return [{"role": "user", "content": first_task}]

        self.assertEqual(self._run(loop), 0)
        self.assertEqual(len(list(self.home.sessions_dir.glob("*/agent.json"))), 1)


if __name__ == "__main__":
    unittest.main()
