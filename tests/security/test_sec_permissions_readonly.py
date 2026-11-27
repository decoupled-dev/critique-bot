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

    # SECURITY FINDING PM-6 (fixed): "git status" via run_command is read-only, so auto mode runs the repo's fsmonitor unasked
    def test_auto_mode_git_status_command_does_not_run_repo_config_programs(self) -> None:
        approvals = Approvals()
        session = Scripted([call("run_command", command="git status"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="auto")
        self.assertFalse(self.marker.exists(), "a read-only git command ran a program named in .git/config")


class RepoConfigHardeningTests(_FsmonitorRepo):
    """Regression tests for PM-6: what run_command's read-only git still meets in .git/config."""

    def test_pager_external_diff_and_hooks_are_switched_off(self) -> None:
        git(self.root, "config", "diff.external", "touch pwned-by-ext-diff; false")
        git(self.root, "config", "core.pager", "touch pwned-by-pager; cat")
        (self.root / "a.txt").write_text("b\n", encoding="utf-8")
        session = Scripted([call("run_command", command="git diff"), call("run_command", command="git log -1 -p"),
                            "COMPLETED"])
        with Approvals().patch():
            self.loop(session, approve_mode="auto")
        for name in ("pwned-by-ext-diff", "pwned-by-pager", "pwned-by-fsmonitor"):
            self.assertFalse((self.root / name).exists(), name)

    def test_a_filter_or_diff_driver_in_the_repo_config_is_not_read_only(self) -> None:
        self.assertTrue(agent_tools.read_only_command("git status", cwd=self.root))
        git(self.root, "config", "filter.x.clean", "touch pwned-by-filter; cat")
        self.assertFalse(agent_tools.read_only_command("git status", cwd=self.root))
        self.assertTrue(agent_tools.read_only_command("git status"))  # the text alone cannot tell

    def test_a_cd_before_git_is_not_read_only_with_a_folder(self) -> None:
        self.assertFalse(agent_tools.read_only_command("cd sub && git log", cwd=self.root))
        self.assertTrue(agent_tools.read_only_command("cd sub && ls", cwd=self.root))

    def test_git_command_env_overrides_the_repo(self) -> None:
        import os
        import subprocess

        env = {**os.environ, **agent_tools.git_command_env()}
        subprocess.run(["git", "status"], cwd=self.root, env=env, capture_output=True, check=True)
        self.assertFalse(self.marker.exists())


class _HelperBase(_FsmonitorRepo):
    """A coordinator with one fake helper tab whose script runs git status."""

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

    @staticmethod
    def helper_ran_git_status() -> bool:
        """True when a helper got git status output back (the fsmonitor marker alone also depends on PM-6)."""
        from test_agent_helpers import _HelperTab

        return any("On branch" in prompt for tab in _HelperTab.instances for prompt in tab.sent)


class HelperTests(_HelperBase):
    def test_ask_mode_asks_before_a_read_only_command(self) -> None:
        # Control: in ask mode the coordinator's own read-only command waits for the user.
        approvals = Approvals()
        session = Scripted([call("run_command", command="git status"), "COMPLETED"])
        with approvals.patch():
            self.loop(session, approve_mode="ask")
        self.assertEqual(len(approvals.asked), 1)
        self.assertFalse(self.marker.exists())

    # SECURITY FINDING PM-7 (fixed): delegate with no files is a "read" step, so in ask mode helpers run commands nobody approved
    def test_ask_mode_delegate_does_not_run_commands_unasked(self) -> None:
        approvals = Approvals()
        delegate = call("delegate", tasks=[{"name": "status", "brief": "Run git status and report what it says."}])
        with approvals.patch():
            self.loop_with_helpers(Scripted([delegate, "COMPLETED"]), "ask")
        self.assertFalse(self.marker.exists(), "a helper ran a command in ask mode with no approval")
        self.assertFalse(self.helper_ran_git_status(), "the helper's git status ran with no approval")

    # SECURITY FINDING PM-7 (fixed): the same in plan mode, which promises that nothing runs until the plan is approved
    def test_plan_mode_delegate_does_not_run_commands_unasked(self) -> None:
        approvals = Approvals()
        delegate = call("delegate", tasks=[{"name": "status", "brief": "Run git status and report what it says."}])
        with approvals.patch(), mock.patch("critique_bot.engine.ui._review_plan", lambda: ("no", "")):
            self.loop_with_helpers(Scripted([delegate, "COMPLETED"]), "plan")
        self.assertFalse(self.marker.exists(), "a helper ran a command in plan mode with no approval")
        self.assertFalse(self.helper_ran_git_status(), "the helper's git status ran with no approval")


class HelperModeTests(_HelperBase):
    """Regression tests for PM-7: helpers follow the coordinator's mode for commands."""

    def test_auto_mode_helpers_still_run_read_only_commands(self) -> None:
        delegate = call("delegate", tasks=[{"name": "status", "brief": "Run git status and report what it says."}])
        with Approvals().patch():
            self.loop_with_helpers(Scripted([delegate, "COMPLETED"]), "auto")
        self.assertTrue(self.helper_ran_git_status())
        self.assertFalse(self.marker.exists())  # PM-6: still with fsmonitor off

    def test_edits_mode_helpers_refuse_commands(self) -> None:
        from test_agent_helpers import _HelperTab

        delegate = call("delegate", tasks=[{"name": "status", "brief": "Run git status and report what it says."}])
        with Approvals().patch():
            self.loop_with_helpers(Scripted([delegate, "COMPLETED"]), "edits")
        self.assertFalse(self.helper_ran_git_status())
        self.assertTrue(any("not run in a helper" in prompt for tab in _HelperTab.instances for prompt in tab.sent))


class AdbTests(SecBase):
    """run_command adb ...: the AOSP pack says device changes always ask."""

    DEVICE_CHANGES = (
        "adb shell dumpsys battery set level 1",         # changes the device's battery state
        "adb shell dumpsys deviceidle force-idle",       # forces doze
        "adb logcat -d -c",                              # clears the device log (the evidence)
        "adb shell ls '>' /sdcard/important.txt",        # the device shell runs the quoted redirect
        "adb shell settings get global x '>' /data/local/tmp/y",
    )

    # SECURITY FINDING PM-8 (fixed): adb commands that change the device are classed read-only (prefix match, device-side redirects)
    def test_device_changing_adb_commands_are_not_read_only(self) -> None:
        wrongly = [command for command in self.DEVICE_CHANGES if agent_tools.read_only_command(command)]
        self.assertEqual(wrongly, [])

    # SECURITY FINDING PM-8 (fixed): so auto mode (and every helper chat) runs them without asking
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


class AdbFormTests(SecBase):
    """Regression tests for PM-8: the adb read-only list, and which commands count as device changes."""

    def test_common_device_reads_stay_read_only(self) -> None:
        for command in (
            "adb shell dumpsys activity activities",
            "adb shell dumpsys meminfo com.example.app",
            "adb -s emulator-5554 logcat -d -v threadtime *:E",
            "adb shell settings get global airplane_mode_on",
            "adb shell pm list packages -3",
            "adb shell wm size",
            "adb devices -l",
        ):
            self.assertTrue(agent_tools.read_only_command(command), command)

    def test_changes_and_device_shell_tricks_are_not_read_only(self) -> None:
        for command in (
            "adb shell wm size 100x100",
            "adb shell settings put global x 1",
            'adb shell "dumpsys battery"',
            "adb shell dumpsys deviceidle whitelist +com.x",
            "adb logcat -G 16M -d",
            "adb logcat -d -f /sdcard/x",
            "adb shell",
            "adb -H evil.example shell getprop",
            "adb shell getprop x\\;reboot",
        ):
            self.assertFalse(agent_tools.read_only_command(command), command)

    def test_device_change_risk(self) -> None:
        self.assertTrue(agent_tools.adb_device_change("adb install app.apk"))
        self.assertTrue(agent_tools.adb_device_change("adb logcat -c"))
        self.assertTrue(agent_tools.adb_device_change("adb shell dumpsys battery set level 1"))
        self.assertEqual(agent_tools.adb_device_change("adb pull /sdcard/x out/x"), "")
        self.assertEqual(agent_tools.adb_device_change("adb logcat"), "")
        self.assertEqual(agent_tools.adb_device_change("adb shell getprop"), "")
        ctx = agent_tools.ToolContext(workspace=self.root)
        perm = agent_tools.permission_for("run_command", {"command": "adb install app.apk"}, ctx)
        self.assertEqual(perm.key, "device:adb")
        self.assertTrue(perm.risk)


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
