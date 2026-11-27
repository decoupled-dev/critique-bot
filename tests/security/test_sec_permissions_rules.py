# SPDX-License-Identifier: Apache-2.0
"""Saved allow/deny rules and "don't ask again": what a prompt-injected model can make them cover.

Each test asserts the SECURE behavior. Open findings are marked ``@unittest.expectedFailure``
with a ``SECURITY FINDING PM-<n>`` line; see plan/results/security/permissions-injection.md.
"""

from __future__ import annotations

import sys
import unittest

from .sec_permissions_support import Approvals, Scripted, SecBase, call, git_repo, use_terminal_ui

from critique_bot import agent_rules


def setUpModule() -> None:
    global _undo_ui
    _undo_ui = use_terminal_ui()


def tearDownModule() -> None:
    _undo_ui()


class AllowRuleTests(SecBase):
    # SECURITY FINDING PM-1 (fixed): a newline in run_command slips any command past a saved allow rule
    def test_allow_rule_does_not_cover_a_second_line(self) -> None:
        # The user allowed "echo ok*". The model appends a second line: bash runs it as a new command.
        approvals = Approvals()
        session = Scripted([call("run_command", command="echo ok\ntouch pwned-by-newline"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="ask", settings={"allow": ["echo ok*"]})
        self.assertFalse((self.root / "pwned-by-newline").exists(), "the second line ran without a question")
        self.assertEqual(len(approvals.asked), 1)

    def test_allow_rule_does_not_cover_a_semicolon_chain(self) -> None:
        # Control: the same attack with ";" is caught (each part needs a rule).
        approvals = Approvals()
        session = Scripted([call("run_command", command="echo ok; touch pwned-by-semicolon"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="ask", settings={"allow": ["echo ok*"]})
        self.assertFalse((self.root / "pwned-by-semicolon").exists())
        self.assertEqual(len(approvals.asked), 1)

    # SECURITY FINDING PM-1 (fixed): the rule matcher sees "echo ok touch x" (newline folded to a space)
    def test_command_segments_keeps_newline_as_separator(self) -> None:
        self.assertIsNone(
            agent_rules.allowed_by(
                agent_rules.load_rules({"allow": ["git status*"]})[0], tool="run_command", perm_kind="command",
                command="git status\nrm -rf src", paths=(), workspace=self.root,
            )
        )


class DenyRuleTests(SecBase):
    # SECURITY FINDING PM-2 (fixed): a newline slips a command past a deny rule ("any part of it matches")
    def test_deny_rule_covers_a_second_line(self) -> None:
        # Deny rules apply in every mode, whatever the user answers.
        approvals = Approvals(default="yes")
        session = Scripted([call("run_command", command="echo hi\ntouch denied-but-ran"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="ask", settings={"deny": ["touch*"]})
        self.assertFalse((self.root / "denied-but-ran").exists(), "a deny rule did not stop the second line")

    def test_deny_rule_covers_a_semicolon_chain(self) -> None:
        approvals = Approvals(default="yes")
        session = Scripted([call("run_command", command="echo hi; touch denied-chain"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="ask", settings={"deny": ["touch*"]})
        self.assertFalse((self.root / "denied-chain").exists())


    def test_deny_rule_covers_subexpressions_wrappers_and_carriage_returns(self) -> None:
        # Regression (PM-2): a deny rule matches a command hidden anywhere the shell would run one.
        rules = agent_rules.load_rules({"deny": ["touch*"]})[0]
        for command in ("echo $(touch x)", "echo `touch x`", "(touch x)", "FOO=1 env touch x", "echo hi\r\ntouch x"):
            with self.subTest(command=command):
                self.assertIsNotNone(agent_rules.denied_by(rules, tool="run_command", perm_kind="command",
                                                           command=command, paths=(), workspace=self.root))

    def test_a_broad_delete_on_a_second_line_is_risky(self) -> None:
        # Regression (PM-1): the risk check splits lines like the shell does.
        from critique_bot import agent_tools

        self.assertTrue(agent_tools.risky_command("echo ok\nrm -rf ."))


class AlwaysAllowTests(SecBase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "README").write_text("hello\n", encoding="utf-8")
        git_repo(self.root)

    def test_always_on_git_log_offers_the_rule_git_log_star(self) -> None:
        # What the "don't ask again" option says it covers.
        self.assertEqual(agent_rules.suggest_rule("git log --oneline -5"), "git log*")

    # SECURITY FINDING PM-3 (fixed): "don't ask again for git log*" silently allows every git command this session
    def test_always_for_git_log_does_not_allow_git_config_and_aliases(self) -> None:
        # The user says "always" to a harmless git log. The injected model then defines a shell alias
        # and runs it: arbitrary code, with no further question.
        approvals = Approvals(answers=["always"], default="no")
        session = Scripted([
            call("run_command", command="git log --oneline -1"),
            call("run_command", command="git config alias.pwn '!touch pwned-by-alias'"),
            call("run_command", command="git pwn"),
            "COMPLETED",
        ])
        with approvals.patch():
            self.loop(session, approve_mode="ask")
        self.assertFalse((self.root / "pwned-by-alias").exists(), "a git alias ran a shell command unasked")
        self.assertGreaterEqual(len(approvals.asked), 2)

    # SECURITY FINDING PM-4 (fixed): the auto-suggested rule "git log*" lets git log --output write any file, anywhere
    def test_git_log_rule_does_not_cover_output_to_any_path(self) -> None:
        target = self.outside / "written-outside.txt"
        approvals = Approvals()
        session = Scripted([
            call("run_command", command=f"git log -1 --output={target} --format=ATTACKER-TEXT"),
            "COMPLETED",
        ])
        with approvals.patch():
            self.loop(session, approve_mode="ask", settings={"allow": ["git log*"]})
        self.assertFalse(target.exists(), "an allow rule for git log wrote a file outside the workspace")
        self.assertEqual(len(approvals.asked), 1)

    # SECURITY FINDING PM-4 (fixed): the same rule can overwrite .git/config (then any git command runs code)
    def test_git_log_rule_cannot_rewrite_git_config(self) -> None:
        before = (self.root / ".git" / "config").read_text(encoding="utf-8")
        session = Scripted([
            call("run_command", command="git log -1 --output=.git/config --format=[core]%n%x09fsmonitor=touch%x20x"),
            "COMPLETED",
        ])
        with Approvals().patch():
            self.loop(session, approve_mode="ask", settings={"allow": ["git log*"]})
        self.assertEqual((self.root / ".git" / "config").read_text(encoding="utf-8"), before)


class RuleScopeTests(SecBase):
    """Regression tests for PM-3 and PM-4: a rule covers what it says, and no option that writes or runs."""

    def allowed(self, patterns: list[str], command: str) -> object:
        rules = agent_rules.load_rules({"allow": patterns})[0]
        return agent_rules.allowed_by(rules, tool="run_command", perm_kind="command", command=command,
                                      paths=(), workspace=self.root)

    def test_rules_never_cover_options_that_write_or_run(self) -> None:
        for patterns, command in (
            (["git log*"], "git log --output=/tmp/x"),
            (["git log*"], "git log --outp=/tmp/x"),
            (["git diff*"], "git diff --ext-diff"),
            (["git *"], "git -c core.pager=sh log"),
            (["git *"], "git -C ../other status"),
            (["git *"], "git rebase --exec 'sh x' HEAD~1"),
            (["git *"], "git fetch --upload-pack=x origin"),
            (["sort*"], "sort -o /tmp/x a.txt"),
            (["find*"], "find . -exec sh {} ;"),
            (["rg*"], "rg --pre ./x foo a.txt"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.allowed(patterns, command))
                self.assertEqual(agent_rules.suggest_rule(command), "")

    def test_rules_still_cover_plain_uses(self) -> None:
        self.assertIsNotNone(self.allowed(["git log*"], "git log --oneline -5"))
        self.assertIsNotNone(self.allowed(["git *"], "git -P log --stat"))
        self.assertIsNotNone(self.allowed(["sort*"], "sort -n a.txt"))

    def test_a_git_rule_does_not_cover_an_alias(self) -> None:
        self.assertIsNone(self.allowed(["git log*"], "git logx"))
        self.assertIsNone(self.allowed(["git *"], "git pwn"))
        # A rule that spells the external command out still covers it.
        self.assertIsNotNone(self.allowed(["git lfs*"], "git lfs pull"))

    def test_no_dont_ask_again_without_a_rule_to_show(self) -> None:
        from critique_bot import agent_tools

        ctx = agent_tools.ToolContext(workspace=self.root)
        perm = agent_tools.permission_for("run_command", {"command": "git log --output=x"}, ctx)
        self.assertEqual((perm.rule, perm.key), ("", ""))
        perm = agent_tools.permission_for("run_command", {"command": "git log -5"}, ctx)
        self.assertEqual(perm.rule, "git log*")


class WrittenCodeTests(SecBase):
    # SECURITY FINDING PM-5: code the task wrote runs through an allowed test/build command that does not name it
    @unittest.expectedFailure
    def test_allowed_test_command_does_not_run_a_test_file_the_task_wrote(self) -> None:
        # Edits mode: edits run unasked. The user allowed the project's test command. The injected
        # model writes a new test file (which could open a socket) and runs the allowed command.
        python = sys.executable
        payload = "import pathlib\npathlib.Path('pwned-by-test-file').write_text('x')\n"
        approvals = Approvals()
        session = Scripted([
            call("write_files", files=[{"path": "test_zz_payload.py", "content": payload}]),
            call("run_command", command=f"{python} -m unittest discover"),
            "COMPLETED",
        ])
        with approvals.patch():
            self.loop(session, approve_mode="edits", settings={"allow": [f"{python} -m unittest*"]})
        self.assertFalse((self.root / "pwned-by-test-file").exists(),
                         "a file this task wrote ran through an allow rule (docs: such a command asks)")

    def test_command_naming_the_written_file_asks(self) -> None:
        # Control: naming the written script directly is caught.
        python = sys.executable
        approvals = Approvals()
        session = Scripted([
            call("write_files", files=[{"path": "tool.py", "content": "open('pwned-direct','w')\n"}]),
            call("run_command", command=f"{python} tool.py"),
            "COMPLETED",
        ])
        with approvals.patch():
            self.loop(session, approve_mode="edits", settings={"allow": [f"{python} tool.py"]})
        self.assertFalse((self.root / "pwned-direct").exists())
        self.assertEqual(len(approvals.asked), 1)


if __name__ == "__main__":
    unittest.main()
