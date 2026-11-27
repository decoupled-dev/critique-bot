# SPDX-License-Identifier: Apache-2.0
""""Read-only" steps that are not: commands auto mode runs unasked, helper chats, and git reads.

Each test asserts the SECURE behavior. Open findings are marked ``@unittest.expectedFailure``
with a ``SECURITY FINDING PM-<n>`` line; see plan/results/security/permissions-injection.md.
"""

from __future__ import annotations

import unittest
from unittest import mock

from .sec_permissions_support import Approvals, Scripted, SecBase, call, git, git_repo, use_terminal_ui

from critique_bot import agent_tools


def setUpModule() -> None:
    global _undo_ui
    _undo_ui = use_terminal_ui()


def tearDownModule() -> None:
    _undo_ui()


class _FsmonitorRepo(SecBase):
    """A workspace whose .git/config names an fsmonitor program: what a project shipped as an archive
    with its .git folder (or a shared checkout) can carry. crit's own git tools switch it off."""

    def setUp(self) -> None:
        super().setUp()
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        git_repo(self.root)
        git(self.root, "config", "core.fsmonitor", "touch pwned-by-fsmonitor; false")
        self.marker = self.root / "pwned-by-fsmonitor"


class RepoConfigTests(_FsmonitorRepo):
    def test_git_status_tool_does_not_run_the_repos_fsmonitor(self) -> None:
        # Control: the git_status tool runs git with core.fsmonitor= (git_argv).
        session = Scripted([call("git_status"), "COMPLETED"])
        with Approvals().patch():
            self.loop(session, approve_mode="auto")
        self.assertFalse(self.marker.exists())

    # SECURITY FINDING PM-6: "git status" via run_command is read-only, so auto mode runs the repo's fsmonitor unasked
    @unittest.expectedFailure
    def test_auto_mode_git_status_command_does_not_run_repo_config_programs(self) -> None:
        approvals = Approvals()
        session = Scripted([call("run_command", command="git status"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="auto")
        self.assertFalse(self.marker.exists(), "a read-only git command ran a program named in .git/config")


class HelperTests(_FsmonitorRepo):
    def loop_with_helpers(self, coordinator: Scripted, mode: str) -> None:
        from test_agent_helpers import _HelperTab

        self.loop(coordinator, approve_mode=mode, helper_factory=_HelperTab, helper_count=1)

    def setUp(self) -> None:
        super().setUp()
        from test_agent_helpers import _HelperTab

        _HelperTab.scripts = {"status": [call("run_command", command="git status"), "COMPLETED Status reported."]}
        _HelperTab.threads = set()
        _HelperTab.instances = []
        _HelperTab.opened = 0

    def test_ask_mode_asks_before_a_read_only_command(self) -> None:
        # Control: in ask mode the coordinator's own read-only command waits for the user.
        approvals = Approvals()
        session = Scripted([call("run_command", command="git status"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="ask")
        self.assertEqual(len(approvals.asked), 1)
        self.assertFalse(self.marker.exists())

    # SECURITY FINDING PM-7: delegate with no files is a "read" step, so in ask mode helpers run commands nobody approved
    @unittest.expectedFailure
    def test_ask_mode_delegate_does_not_run_commands_unasked(self) -> None:
        approvals = Approvals()
        delegate = call("delegate", tasks=[{"name": "status", "brief": "Run git status and report what it says."}])
        with approvals.patch():
            self.loop_with_helpers(Scripted([delegate, "COMPLETED"]), "ask")
        self.assertFalse(self.marker.exists(), "a helper ran a command in ask mode with no approval")

    # SECURITY FINDING PM-7: the same in plan mode, which promises that nothing runs until the plan is approved
    @unittest.expectedFailure
    def test_plan_mode_delegate_does_not_run_commands_unasked(self) -> None:
        approvals = Approvals()
        delegate = call("delegate", tasks=[{"name": "status", "brief": "Run git status and report what it says."}])
        with approvals.patch(), mock.patch("critique_bot.engine.ui._review_plan", lambda: ("no", "")):
            self.loop_with_helpers(Scripted([delegate, "COMPLETED"]), "plan")
        self.assertFalse(self.marker.exists(), "a helper ran a command in plan mode with no approval")


class AdbTests(SecBase):
    """run_command adb ...: the AOSP pack says device changes always ask."""

    DEVICE_CHANGES = (
        "adb shell dumpsys battery set level 1",         # changes the device's battery state
        "adb shell dumpsys deviceidle force-idle",       # forces doze
        "adb logcat -d -c",                              # clears the device log (the evidence)
        "adb shell ls '>' /sdcard/important.txt",        # the device shell runs the quoted redirect
        "adb shell settings get global x '>' /data/local/tmp/y",
    )

    # SECURITY FINDING PM-8: adb commands that change the device are classed read-only (prefix match, device-side redirects)
    @unittest.expectedFailure
    def test_device_changing_adb_commands_are_not_read_only(self) -> None:
        wrongly = [command for command in self.DEVICE_CHANGES if agent_tools.read_only_command(command)]
        self.assertEqual(wrongly, [])

    # SECURITY FINDING PM-8: so auto mode (and every helper chat) runs them without asking
    @unittest.expectedFailure
    def test_auto_mode_asks_before_changing_the_device(self) -> None:
        approvals = Approvals()
        # A fake runner: no real adb is started.
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=b"", stderr=b""))
        session = Scripted([call("run_command", command="adb shell dumpsys battery set level 1"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="auto", runner=runner)
        self.assertEqual(len(approvals.asked), 1)
        runner.assert_not_called()

    def test_plain_device_reads_stay_read_only(self) -> None:
        for command in ("adb devices", "adb shell getprop ro.build.fingerprint", "adb logcat -d"):
            self.assertTrue(agent_tools.read_only_command(command), command)


class GitSecretTests(SecBase):
    SECRET = "fake-not-real-secret-222"

    def setUp(self) -> None:
        super().setUp()
        (self.root / ".env").write_text("MY_DB_PASS=fake-not-real-secret-111\n", encoding="utf-8")
        git_repo(self.root)
        (self.root / ".env").write_text(f"MY_DB_PASS={self.SECRET}\n", encoding="utf-8")

    def test_read_files_of_env_asks(self) -> None:
        # Control: reading .env directly asks first.
        approvals = Approvals()
        session = Scripted([call("read_files", path=".env"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="auto")
        self.assertEqual(len(approvals.asked), 1)
        self.assertNotIn(self.SECRET, "\n".join(session.sent))

    # SECURITY FINDING PM-11: git_diff (a "read", unasked in every mode) shows a tracked .env's new contents to the model
    @unittest.expectedFailure
    def test_git_diff_does_not_show_a_secret_file_unasked(self) -> None:
        approvals = Approvals()
        session = Scripted([call("git_diff"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="plan")
        self.assertTrue(approvals.asked or self.SECRET not in "\n".join(session.sent),
                        "the secret went to the model with no question")

    # SECURITY FINDING PM-11: git_show patch=true shows a committed .env the same way
    @unittest.expectedFailure
    def test_git_show_patch_does_not_show_a_secret_file_unasked(self) -> None:
        git(self.root, "commit", "-q", "-am", "rotate")
        approvals = Approvals()
        session = Scripted([call("git_show", rev="HEAD", patch=True), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="ask")
        self.assertTrue(approvals.asked or self.SECRET not in "\n".join(session.sent),
                        "the secret went to the model with no question")


if __name__ == "__main__":
    unittest.main()
