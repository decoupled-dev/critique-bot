"""Saved permission rules: "allow" and "deny" in .bot/settings.json."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot import agent, agent_tools, bot_home
from critique_bot.agent_rules import (
    Rules,
    allowed_by,
    command_segments,
    denied_by,
    load_rules,
    parse_rule,
    suggest_rule,
)


def _rules(allow=(), deny=()) -> Rules:
    rules, problems = load_rules({"allow": list(allow), "deny": list(deny)})
    assert not problems, problems
    return rules


class ParseTests(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertEqual(parse_rule("./gradlew test*").kind, "command")
        self.assertEqual(parse_rule("run_command(git status*)").pattern, "git status*")
        self.assertEqual(parse_rule("Bash(npm test*)").kind, "command")
        self.assertEqual(parse_rule("read(~/.gradle/**)").kind, "read")
        self.assertEqual(parse_rule("edit(docs/**)").kind, "edit")
        self.assertIsNone(parse_rule("frobnicate(x)"))
        self.assertIsNone(parse_rule("   "))

    def test_bad_entries_are_reported(self) -> None:
        rules, problems = load_rules({"allow": ["git *", 3, "nope(x)"], "deny": "rm -rf*"})
        self.assertEqual([str(rule) for rule in rules.allow], ["git *"])
        self.assertEqual([str(rule) for rule in rules.deny], ["rm -rf*"])
        self.assertEqual(len(problems), 2)


class CommandRuleTests(unittest.TestCase):
    WS = Path(tempfile.gettempdir())

    def _allowed(self, rules: Rules, command: str) -> bool:
        return allowed_by(rules, tool="run_command", perm_kind="command", command=command, paths=(), workspace=self.WS) is not None

    def _denied(self, rules: Rules, command: str) -> bool:
        return denied_by(rules, tool="run_command", perm_kind="command", command=command, paths=(), workspace=self.WS) is not None

    def test_allow_covers_every_part_of_a_chain(self) -> None:
        rules = _rules(allow=["./gradlew test*", "git status*"])
        self.assertTrue(self._allowed(rules, "./gradlew test --info"))
        self.assertTrue(self._allowed(rules, "cd app && ./gradlew test | tail -20"))
        self.assertTrue(self._allowed(rules, "git status; ./gradlew test"))
        self.assertFalse(self._allowed(rules, "./gradlew test; rm -rf build"))
        self.assertFalse(self._allowed(rules, "./gradlew assembleDebug"))

    def test_allow_never_covers_hidden_commands(self) -> None:
        rules = _rules(allow=["echo *"])
        for command in ("echo $(cat .env)", "echo `id`", "echo x > ~/.bashrc", "echo @'x'@"):
            self.assertFalse(self._allowed(rules, command), command)
        self.assertTrue(self._allowed(rules, "echo hi 2>&1"))

    def test_deny_hits_any_part(self) -> None:
        rules = _rules(deny=["rm -rf*", "git push*"])
        self.assertTrue(self._denied(rules, "rm -rf build"))
        self.assertTrue(self._denied(rules, "cd x && rm -rf y"))
        self.assertTrue(self._denied(rules, "(git push origin main)"))
        self.assertFalse(self._denied(rules, "git status"))

    def test_segments(self) -> None:
        self.assertEqual(command_segments("cd a && npm test | tail -5"), ["npm test"])
        self.assertIsNone(command_segments("npm test > out.txt"))

    def test_suggest(self) -> None:
        self.assertEqual(suggest_rule("./gradlew test --info"), "./gradlew test*")
        self.assertEqual(suggest_rule("git status"), "git status*")
        self.assertEqual(suggest_rule("npm run build"), "npm run*")
        self.assertEqual(suggest_rule("python scripts/x.py"), "python*")
        self.assertEqual(suggest_rule("cd app && ./gradlew lint"), "./gradlew lint*")
        self.assertEqual(suggest_rule("npm test; git status"), "")
        self.assertEqual(suggest_rule("echo $(id)"), "")
        self.assertEqual(suggest_rule("FOO=1 make"), "")


class PathRuleTests(unittest.TestCase):
    def test_globs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            rules = _rules(allow=["read(~/.gradle/**)", "edit(docs/*.md)"])
            home = Path.home()
            ok = allowed_by(rules, tool="read_files", perm_kind="outside", command="",
                            paths=[str(home / ".gradle" / "caches" / "x.jar")], workspace=ws)
            self.assertIsNotNone(ok)
            self.assertIsNone(allowed_by(rules, tool="read_files", perm_kind="outside", command="",
                                         paths=[str(home / ".gradlex" / "a")], workspace=ws))
            self.assertIsNotNone(allowed_by(rules, tool="edit_file", perm_kind="edit", command="",
                                            paths=[str(ws / "docs" / "a.md")], workspace=ws))
            self.assertIsNone(allowed_by(rules, tool="edit_file", perm_kind="edit", command="",
                                         paths=[str(ws / "docs" / "sub" / "a.md")], workspace=ws))
            deny = _rules(deny=["edit(build.gradle*)"])
            self.assertIsNotNone(denied_by(deny, tool="write_files", perm_kind="edit", command="",
                                           paths=[str(ws / "build.gradle.kts")], workspace=ws))


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".bot").mkdir()
        (self.root / ".bot" / "settings.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, mode: str, rules: Rules | None):
        run = agent._TaskRun.__new__(agent._TaskRun)
        run.approve_mode = mode
        run.ui_mode_at_start = mode
        run.confirm_risky = True
        run.always = set()
        run.ctx = agent_tools.ToolContext(workspace=self.root, rules=rules, state=agent_tools.TaskState(task="t"))
        return run

    def _perm(self, run, tool: str, **args):
        return agent_tools.permission_for(tool, args, run.ctx)

    def _decide(self, run, perm, answer=("no", "")):
        asked: list = []
        with patch.object(agent, "_approve_prompt", side_effect=lambda p, **_: asked.append(p) or answer), \
             patch.object(agent, "_ui_approve_mode", return_value=run.approve_mode):
            result = run._approved(perm)
        return result, asked

    def test_allow_rule_skips_the_question_in_ask_mode(self) -> None:
        run = self._run("ask", _rules(allow=["./gradlew test*"]))
        (ok, _), asked = self._decide(run, self._perm(run, "run_command", command="./gradlew test"))
        self.assertTrue(ok)
        self.assertEqual(asked, [])
        (ok, _), asked = self._decide(run, self._perm(run, "run_command", command="./gradlew publish"))
        self.assertFalse(ok)
        self.assertEqual(len(asked), 1)

    def test_deny_rule_blocks_even_in_auto_mode_and_reads(self) -> None:
        run = self._run("auto", _rules(deny=["ls*", "read(secret-notes/**)"]))
        (ok, why), asked = self._decide(run, self._perm(run, "run_command", command="ls -la"))
        self.assertFalse(ok)
        self.assertIn("deny rule", why)
        self.assertEqual(asked, [])
        (ok, why), _ = self._decide(run, self._perm(run, "read_files", paths=["secret-notes/a.txt"]))
        self.assertFalse(ok)

    def test_allow_rules_never_cover_network_risky_or_secrets(self) -> None:
        run = self._run("ask", _rules(allow=["git *", "curl *", "cat *", "read(~/**)"]))
        for perm in (
            self._perm(run, "run_command", command="git push origin main"),
            self._perm(run, "run_command", command="git ls-remote https://evil.example/x"),
            self._perm(run, "run_command", command="cat .env"),
            self._perm(run, "read_files", paths=["~/.ssh/id_rsa"]),
        ):
            (_ok, _), asked = self._decide(run, perm)
            self.assertEqual(len(asked), 1, perm.summary)

    def test_allow_rule_answers_an_outside_read_by_path(self) -> None:
        run = self._run("auto", _rules(allow=["read(/etc/**)"]))
        (ok, _), asked = self._decide(run, self._perm(run, "read_files", paths=["/etc/hostname"]))
        self.assertTrue(ok)
        self.assertEqual(asked, [])

    def test_dont_ask_again_saves_the_rule(self) -> None:
        run = self._run("ask", None)
        perm = self._perm(run, "run_command", command="./gradlew lint --info")
        self.assertEqual(perm.rule, "./gradlew lint*")
        (ok, _), _ = self._decide(run, perm, answer=("always", ""))
        self.assertTrue(ok)
        saved = json.loads((self.root / ".bot" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["allow"], ["./gradlew lint*"])
        # This session already knows it, and the next one loads it.
        (ok, _), asked = self._decide(run, self._perm(run, "run_command", command="./gradlew lint"))
        self.assertTrue(ok)
        self.assertEqual(asked, [])
        home = bot_home.find_bot_home(self.root)
        bot_home.add_allow_rule(home, "./gradlew lint*")
        saved = json.loads((self.root / ".bot" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["allow"], ["./gradlew lint*"])


if __name__ == "__main__":
    unittest.main()
