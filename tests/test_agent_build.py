"""Builds: long timeouts, summaries, hints, retries, artifacts, JDK/SDK discovery, hand-backs."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from critique_bot import agent, agent_build, agent_shell, agent_tools, agent_ui
from critique_bot.agent_shell import Shell, ShellSession

_GRADLE_FAILURE = """\
> Task :app:compileDebugKotlin FAILED
e: file:///C:/src/app/src/main/java/com/x/MainActivity.kt:12:5 Unresolved reference: foo

FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':app:compileDebugKotlin'.
> A failure occurred while executing org.jetbrains.kotlin.compilerRunner.GradleCompilerRunnerWithWorkers$GradleKotlinCompilerWorkAction
   > Compilation error. See log for more details

* Try:
> Run with --stacktrace option to get the stack trace.

BUILD FAILED in 41s
"""


class CommandKindTests(unittest.TestCase):
    def test_builds(self) -> None:
        for command in [
            ".\\gradlew.bat assembleDebug", "./gradlew :app:assembleDebug --stacktrace", "gradle build",
            "cd app; .\\gradlew.bat test", "m -j32 CarService", "mvn -q package", "npm install", "npm ci",
            "cmake --build out", "$env:JAVA_HOME='C:\\jdk'; .\\gradlew.bat assembleDebug", "JAVA_HOME=/jdk ./gradlew build",
            "atest CarServiceUnitTest", "./build.sh", "python -m pip install -r requirements.txt",
        ]:
            with self.subTest(command=command):
                self.assertTrue(agent_build.is_build_command(command))

    def test_not_builds(self) -> None:
        for command in ["git status", "ls -la", "Get-ChildItem", "echo build", "cat build.gradle", "python app.py", "npm --version"]:
            with self.subTest(command=command):
                self.assertFalse(agent_build.is_build_command(command))


class SummaryAndHintTests(unittest.TestCase):
    def test_summary_keeps_what_went_wrong_and_errors(self) -> None:
        lines = agent_build.summarize(_GRADLE_FAILURE)
        text = "\n".join(lines)
        self.assertIn("What went wrong:", text)
        self.assertIn("Execution failed for task ':app:compileDebugKotlin'.", text)
        self.assertIn("Unresolved reference: foo", text)
        self.assertIn("BUILD FAILED in 41s", text)
        self.assertNotIn("--stacktrace", text)
        self.assertEqual(agent_build.summarize("hello\nworld\n"), [])

    def test_hints_for_common_android_failures(self) -> None:
        ctx = agent_build.Context(
            windows=True,
            wrapper=".\\gradlew.bat",
            android_sdk="C:\\Users\\me\\AppData\\Local\\Android\\Sdk",
            jdks=((21, "C:\\Program Files\\Java\\jdk-21"), (17, "C:\\Program Files\\Android\\Android Studio\\jbr")),
        )
        cases = {
            "SDK location not found. Define a valid SDK location with an ANDROID_HOME environment variable": "sdk.dir=C\\:\\\\Users",
            "Android Gradle plugin requires Java 17 to run. You are currently using Java 11.": "$env:JAVA_HOME = 'C:\\Program Files\\Java\\jdk-21'",
            "PKIX path building failed: unable to find valid certification path to requested target": "Windows-ROOT",
            "Timeout waiting to lock journal cache (C:\\Users\\me\\.gradle\\caches\\journal-1)": ".\\gradlew.bat --stop",
            "Failed to install the following Android SDK packages as some licences have not been accepted.": "--licenses",
            "'gradlew' is not recognized as the name of a cmdlet, function, script file": ".\\gradlew.bat",
            "Execution failed for task ':app:compileDebugKotlin'.": "fix the e:/error: lines",
        }
        for output, expected in cases.items():
            with self.subTest(output=output[:40]):
                self.assertTrue(any(expected in hint for hint in agent_build.hints(output, ctx)), agent_build.hints(output, ctx))
        self.assertEqual(agent_build.hints("BUILD SUCCESSFUL in 3s", ctx), [])

    def test_transient_failures(self) -> None:
        self.assertEqual(
            agent_build.transient_failure("./gradlew build", "Timeout waiting to lock build cache"),
            ("Gradle files were locked or its daemon stopped", True),
        )
        self.assertEqual(
            agent_build.transient_failure("./gradlew build", "Could not GET 'https://dl.google.com/x'. Read timed out")[1],
            False,
        )
        self.assertIsNone(agent_build.transient_failure("./gradlew build", _GRADLE_FAILURE))
        self.assertIsNone(agent_build.transient_failure("./gradlew build", "PKIX path building failed; Read timed out"))
        self.assertIsNone(agent_build.transient_failure("pytest", "Read timed out"))

    def test_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start = time.time()
            apk = root / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
            apk.parent.mkdir(parents=True)
            apk.write_bytes(b"x" * 3000)
            old = root / "lib" / "build" / "outputs" / "apk" / "debug" / "old.apk"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"x")
            os.utime(old, (start - 3600, start - 3600))
            self.assertEqual(agent_build.artifacts(root, since=start), [("app/build/outputs/apk/debug/app-debug.apk", 3000)])
            self.assertEqual(agent_build.size_text(3000), "3 KB")


class ToolchainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _jdk(self, name: str, version: str, plat: str = "linux") -> Path:
        home = self.root / "jdks" / name
        (home / "bin").mkdir(parents=True)
        (home / "bin" / ("javac.exe" if plat == "win32" else "javac")).write_text("")
        (home / "release").write_text(f'JAVA_VERSION="{version}"\n')
        return home

    def _android_project(self, agp: str, gradle: str) -> Path:
        project = self.root / "proj"
        (project / "gradle" / "wrapper").mkdir(parents=True)
        (project / "settings.gradle.kts").write_text("")
        (project / "build.gradle.kts").write_text(f'plugins {{ id("com.android.application") version "{agp}" apply false }}\n')
        (project / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
            f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{gradle}-bin.zip\n"
        )
        return project

    def test_versions_and_range(self) -> None:
        self.assertEqual(agent_shell.jdk_version(self._jdk("a", "17.0.9")), 17)
        self.assertEqual(agent_shell.jdk_version(self._jdk("b", "1.8.0_392")), 8)
        project = self._android_project("8.2.0", "8.2")
        self.assertEqual(agent_shell.agp_version(project), (8, 2))
        low, high, why = agent_shell.project_java_range(project)
        self.assertEqual((low, high), (17, 19))
        self.assertIn("AGP 8.2", why)
        toml = self.root / "cat"
        (toml / "gradle").mkdir(parents=True)
        (toml / "gradle" / "libs.versions.toml").write_text('[versions]\nagp = "7.4.2"\n')
        self.assertEqual(agent_shell.agp_version(toml), (7, 4))

    def test_choose_jdk_prefers_android_studio_then_oldest_fitting(self) -> None:
        project = self._android_project("8.2.0", "8.5")
        jdk11 = self._jdk("jdk-11", "11.0.2")
        jdk17 = self._jdk("jdk-17", "17.0.9")
        jdk21 = self._jdk("jdk-21", "21.0.1")
        studio = self._jdk("jbr", "17.0.6")
        found = (
            agent_shell.Jdk(jdk21, 21), agent_shell.Jdk(jdk17, 17), agent_shell.Jdk(studio, 17, "Android Studio"),
            agent_shell.Jdk(jdk11, 11),
        )
        with mock.patch.object(agent_shell, "installed_jdks", return_value=found):
            chosen, note = agent_shell.choose_jdk({"JAVA_HOME": str(jdk11)}, project, "linux")
            self.assertEqual(chosen.home, studio)
            self.assertIn("JAVA_HOME is JDK 11", note)
            chosen, note = agent_shell.choose_jdk({"JAVA_HOME": str(jdk21)}, project, "linux")
            self.assertEqual((chosen.home, note), (jdk21, ""))
        with mock.patch.object(agent_shell, "installed_jdks", return_value=found[:2] + found[3:]):
            chosen, _ = agent_shell.choose_jdk({}, project, "linux")
            self.assertEqual(chosen.home, jdk17)
            env = agent_shell.project_toolchain({"PATH": "/usr/bin"}, project, "linux")
            self.assertEqual(env["JAVA_HOME"], str(jdk17))
            self.assertTrue(env["PATH"].startswith(str(jdk17 / "bin")))

    def test_android_sdk_from_local_properties_and_localappdata(self) -> None:
        sdk = self.root / "Sdk"
        (sdk / "platforms").mkdir(parents=True)
        project = self.root / "p"
        project.mkdir()
        escaped = str(sdk).replace("\\", "\\\\").replace(":", "\\:")
        (project / "local.properties").write_text(f"sdk.dir={escaped}\n")
        self.assertEqual(agent_shell.android_sdk({}, scan=True, workspace=project), str(sdk))
        local = self.root / "Local"
        (local / "Android" / "Sdk" / "platform-tools").mkdir(parents=True)
        self.assertEqual(
            agent_shell.android_sdk({"LOCALAPPDATA": str(local), "ANDROID_HOME": str(self.root / "gone")}, scan=True),
            str(local / "Android" / "Sdk"),
        )


class WindowsCommandTests(unittest.TestCase):
    def test_bare_wrapper_gets_the_powershell_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gradlew.bat").write_text("")
            pwsh = Shell("pwsh", "C:\\pwsh\\pwsh.exe", "PowerShell 7")
            cmd = Shell("cmd", "C:\\Windows\\System32\\cmd.exe", "cmd")
            with mock.patch.object(agent_shell, "_is_windows_shell", return_value=True):
                self.assertEqual(
                    agent_shell.adjust_command("gradlew assembleDebug", pwsh, root), ".\\gradlew.bat --console=plain assembleDebug"
                )
                self.assertEqual(
                    agent_shell.adjust_command("cd app; ./gradlew build", pwsh, root), "cd app; .\\gradlew.bat --console=plain build"
                )
                self.assertEqual(agent_shell.adjust_command("./gradlew build", cmd, root), "gradlew.bat --console=plain build")
            self.assertEqual(agent_shell.adjust_command("./gradlew build"), "./gradlew --console=plain build")

    def test_powershell_native_error_noise_is_removed(self) -> None:
        raw = (
            "gradlew.bat : warning: [options] source value 8 is obsolete\n"
            "At line:1 char:1\n"
            "+ .\\gradlew.bat assembleDebug 2>&1\n"
            "+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n"
            "    + CategoryInfo          : NotSpecified: (warning: [opti...lete:String) [], RemoteException\n"
            "    + FullyQualifiedErrorId : NativeCommandError\n"
            " \n"
            "BUILD SUCCESSFUL in 9s"
        )
        self.assertEqual(
            agent_shell.tidy(raw), "warning: [options] source value 8 is obsolete\n\nBUILD SUCCESSFUL in 9s"
        )


@unittest.skipIf(sys.platform == "win32" or not shutil.which("bash"), "needs bash")
class RunCommandBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        bash = Shell("bash", shutil.which("bash") or "bash", "bash")
        self.session = ShellSession(self.root, bash, available={"bash": bash}, env_extra={"JAVA_HOME": ""})
        self.ctx = agent_tools.ToolContext(workspace=self.root, session=self.session, shell=bash)
        gradlew = self.root / "gradlew"
        gradlew.write_text(
            "#!/bin/sh\n"
            'case " $* " in *" --stop "*) echo stopped >> calls; exit 0;; esac\n'
            "echo run >> calls\n"
            'if [ "$(grep -c run calls)" = "1" ]; then echo "Timeout waiting to lock journal cache" >&2; exit 1; fi\n'
            "mkdir -p app/build/outputs/apk/debug && echo apk > app/build/outputs/apk/debug/app-debug.apk\n"
            'echo "BUILD SUCCESSFUL in 1s"\n'
        )
        gradlew.chmod(0o755)

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def test_lock_failure_is_retried_after_stop_and_artifacts_are_listed(self) -> None:
        result = agent_tools.execute("run_command", {"command": "./gradlew assembleDebug"}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertIn("retried once: Gradle files were locked", result["output"])
        self.assertIn("ARTIFACTS", result["output"])
        self.assertIn("app/build/outputs/apk/debug/app-debug.apk", result["output"])
        self.assertEqual((self.root / "calls").read_text().split(), ["run", "stopped", "run"])
        self.assertEqual(self.session.history[0]["retried"], "Gradle files were locked or its daemon stopped")
        self.assertIn("retried once", result["ui"]["summary"])

    def test_build_gets_the_long_timeout(self) -> None:
        seen: list[float] = []
        real = self.session.run

        def spy(command, **kwargs):
            seen.append(kwargs["timeout"])
            return real("true", **{k: v for k, v in kwargs.items() if k != "timeout"}, timeout=5)

        with mock.patch.object(self.session, "run", spy):
            agent_tools.execute("run_command", {"command": "./gradlew build", "timeout": 120}, self.ctx)
            agent_tools.execute("run_command", {"command": "echo hi", "timeout": 120}, self.ctx)
        self.assertEqual(seen, [agent_build.DEFAULT_BUILD_TIMEOUT, 120.0])

    def test_failed_build_has_summary_and_hints_first(self) -> None:
        (self.root / "fail.sh").write_text(
            "printf '%s\\n' " + " ".join(f"'noise {index}'" for index in range(400)) + "\n"
            "cat <<'EOF'\n" + _GRADLE_FAILURE + "EOF\nexit 1\n"
        )
        result = agent_tools.execute("run_command", {"command": "sh fail.sh; ./gradlew -v >/dev/null; exit 1"}, self.ctx)
        self.assertFalse(result["ok"])
        output = result["output"]
        self.assertLess(output.index("SUMMARY"), output.index("noise 0"))
        self.assertIn("Unresolved reference: foo", output[: output.index("noise 0")])
        self.assertIn("HINTS (from crit", output)

    def test_timeout_reports_the_background_job(self) -> None:
        result = agent_tools.execute("run_command", {"command": "echo start; sleep 3; echo done", "timeout": 1}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertIn("continues as background job b1", result["output"])
        self.assertIn("command_output", result["output"])


class HandBackTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100, theme=agent_ui.rich_theme("dark")))

    def tearDown(self) -> None:
        agent_ui.reset()

    def test_markers(self) -> None:
        for text in [
            "BLOCKED: please build the APK manually in Android Studio.",
            "You can run .\\gradlew.bat assembleDebug on your machine.",
            "FAILED. I cannot run Gradle here. Run the following command yourself.",
            "Open the project in Android Studio and build it.",
        ]:
            with self.subTest(text=text):
                self.assertTrue(agent._hands_back(text))
        for text in ["COMPLETED", "The build passed and the APK is at app/build/outputs/apk/debug/app-debug.apk."]:
            with self.subTest(text=text):
                self.assertFalse(agent._hands_back(text))

    def test_hand_back_is_sent_back_to_run_it(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self.replies = [
                    "BLOCKED: please build the APK manually in Android Studio.",
                    "<tool_call>\n" + json.dumps({"tool": "run_command", "arguments": {"command": "echo built"}}) + "\n</tool_call>",
                    "COMPLETED",
                ]

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self.replies.pop(0)

        session = Session()
        outcome: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            agent.run_agent_loop(
                session, workspace=Path(tmp), index_path=None, cache_dir=None, first_task="build the debug apk",
                max_rounds=10, max_result_chars=8000, read_message=lambda: None, emit=lambda _t: None,
                approve_mode="auto", outcome=outcome,
            )
        self.assertIn("Do not hand the work to the user", session.sent[1])
        self.assertEqual(outcome, ["COMPLETED"])


class ActivityTests(unittest.TestCase):
    def test_activity_text(self) -> None:
        self.assertEqual(agent_ui.activity_text({"run_command": 1}), "Running 1 shell command")
        self.assertEqual(agent_ui.activity_text({"run_command": 2}), "Running 2 shell commands")
        self.assertEqual(agent_ui.activity_text({"read_files": 3, "search_code": 1}), "Reading 3 files, searching 1 pattern")
        self.assertEqual(agent_ui.activity_text({}), "")

    def test_live_line_shows_running_and_elapsed(self) -> None:
        agent_ui.reset()
        console = Console(file=io.StringIO(), force_terminal=False, width=100, record=True, theme=agent_ui.rich_theme("dark"))
        agent_ui.set_console(console)
        with mock.patch.object(agent_ui, "is_tty", return_value=False):
            agent_ui.tool_start("run_command", {"command": ".\\gradlew.bat assembleDebug"})
            agent_ui.state().active_since = time.monotonic() - 75
            console.print(agent_ui.live_renderable())
            agent_ui.tool_done("run_command", {"command": "x"}, {"ok": True, "output": "exit 0"})
        text = console.export_text()
        self.assertIn("Running 1 shell command", text)
        self.assertIn("1m 15s", text)
        self.assertEqual(agent_ui.state().active, {})
        agent_ui.reset()


if __name__ == "__main__":
    unittest.main()


class OldCodeGraphCleanupTests(unittest.TestCase):
    def test_the_unpacked_copy_is_removed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old = home / ".config" / "critique-bot" / "codegraph" / "current" / "bin"
            old.mkdir(parents=True)
            (old / "codegraph").write_text("x")
            keep = home / ".config" / "critique-bot" / "other.txt"
            keep.write_text("keep")
            with mock.patch.object(agent.sys, "platform", "linux"), mock.patch.object(agent.Path, "home", return_value=home):
                agent._remove_old_codegraph()
                agent._remove_old_codegraph()  # nothing left: no error
            self.assertFalse((home / ".config" / "critique-bot" / "codegraph").exists())
            self.assertTrue(keep.exists())
