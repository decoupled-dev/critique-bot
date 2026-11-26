"""AOSP and device defaults: module lookup, lunch targets, envsetup, bare m, logcat, adb hints.

The tree, m, lunch, and adb here are fakes, so no AOSP checkout or device is needed.
"""

from __future__ import annotations

import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import agent_build, agent_shell, file_catalog, soong
from critique_bot.agent_shell import Shell, ShellSession
from critique_bot.agent_tools import TaskState, ToolContext, execute

POSIX = os.name == "posix"

ROOT_BP = textwrap.dedent('''
    // a comment with "quotes" and src/**/*.java inside it
    car_srcs = ["src/**/*.java"]
    car_srcs += ["extra/**/*.kt"]

    filegroup {
        name: "car-service-srcs",
        srcs: car_srcs,
        exclude_srcs: ["src/**/Excluded.java"],
    }

    java_defaults {
        name: "car-defaults",
        srcs: [":car-service-srcs"],
    }

    android_app {
        name: "CarServiceUpdatable",
        defaults: ["car-defaults"],
    }

    /* block comment: src/**/*.java */
    cc_library_shared {
        name: "libcarjni",
        srcs: ["jni/**/*.cpp"],
    }
''')


def _tree(root: Path) -> None:
    (root / ".repo").mkdir()
    (root / ".repo" / "project.list").write_text("packages/services/Car\nframeworks/base\nbuild/make\n")
    car = root / "packages/services/Car/service"
    (car / "src/com/android/car").mkdir(parents=True)
    (car / "src/com/android/car/CarPropertyService.java").write_text("class CarPropertyService {}\n")
    (car / "src/com/android/car/Excluded.java").write_text("class Excluded {}\n")
    (car / "jni").mkdir()
    (car / "jni/Evs.cpp").write_text("int x;\n")
    (car / "Android.bp").write_text(ROOT_BP)
    core = root / "frameworks/base/core/java"
    (core / "android/os").mkdir(parents=True)
    (core / "android/os/Handler.java").write_text("class Handler {}\n")
    (core / "Android.bp").write_text('filegroup {\n    name: "framework-core-sources",\n    srcs: ["**/*.java"],\n}\n')
    (root / "frameworks/base/Android.bp").write_text(textwrap.dedent('''
        filegroup {
            name: "framework-non-updatable-sources",
            device_common_srcs: [":framework-core-sources"],
        }
        java_library {
            name: "framework-minus-apex",
            srcs: [":framework-non-updatable-sources"],
        }
    '''))
    products = root / "device/generic/car"
    products.mkdir(parents=True)
    (products / "AndroidProducts.mk").write_text(
        "COMMON_LUNCH_CHOICES := \\\n    sdk_car_x86_64-trunk_staging-userdebug \\\n    aosp_phone-trunk_staging-eng\n"
    )
    (root / "build").mkdir(exist_ok=True)
    (root / "build" / "envsetup.sh").write_text(textwrap.dedent('''
        lunch() { export TARGET_PRODUCT="${1%%-*}"; export TARGET_BUILD_VARIANT="${1##*-}"; echo "lunched $1"; }
        m() { echo "M $* TARGET=$TARGET_PRODUCT"; }
    '''))


class SoongTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _tree(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_filegroup_defaults_and_variables_lead_to_the_app(self) -> None:
        owner = soong.modules_for_file(self.root, "packages/services/Car/service/src/com/android/car/CarPropertyService.java")
        self.assertEqual([m.name for m in owner.modules], ["CarServiceUpdatable"])
        self.assertIn("filegroup car-service-srcs", owner.via)

    def test_exclude_srcs_and_native_code(self) -> None:
        self.assertIsNone(soong.modules_for_file(self.root, "packages/services/Car/service/src/com/android/car/Excluded.java"))
        owner = soong.modules_for_file(self.root, "packages/services/Car/service/jni/Evs.cpp")
        self.assertEqual([m.name for m in owner.modules], ["libcarjni"])

    def test_filegroup_used_from_a_parent_folder(self) -> None:
        owner = soong.modules_for_file(self.root, "frameworks/base/core/java/android/os/Handler.java")
        self.assertEqual([m.name for m in owner.modules], ["framework-minus-apex"])
        self.assertIn("built by framework-minus-apex", soong.describe_owner(self.root, "frameworks/base/core/java/android/os/Handler.java"))

    def test_lunch_choices_car_first(self) -> None:
        self.assertEqual(soong.lunch_choices(self.root)[0], "sdk_car_x86_64-trunk_staging-userdebug")


@unittest.skipUnless(POSIX and agent_shell.shutil.which("bash"), "needs bash")
class AospCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _tree(self.root)
        file_catalog.forget(self.root)
        bash = Shell("bash", agent_shell.shutil.which("bash") or "bash", "bash")
        self.session = ShellSession(self.root, bash, available={"bash": bash}, env_extra={"TARGET_PRODUCT": None})
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"), session=self.session)

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def _run(self, command: str) -> dict:
        return execute("run_command", {"command": command}, self.ctx)

    def test_m_needs_a_lunch_target_then_just_works(self) -> None:
        first = self._run("m CarServiceUpdatable")
        self.assertFalse(first["ok"])
        self.assertIn("lunch sdk_car_x86_64-trunk_staging-userdebug", first["error"])
        self.assertTrue(self._run("lunch sdk_car_x86_64-trunk_staging-userdebug")["ok"])
        built = self._run("m CarServiceUpdatable")
        self.assertTrue(built["ok"], built)
        self.assertIn("M CarServiceUpdatable TARGET=sdk_car_x86_64", built["output"])

    def test_bare_m_after_an_edit_names_the_module_once(self) -> None:
        self._run("lunch sdk_car_x86_64-trunk_staging-userdebug")
        path = "packages/services/Car/service/src/com/android/car/NewHelper.java"
        written = execute("write_files", {"path": path, "content": "class NewHelper {}\n"}, self.ctx)
        self.assertTrue(written["ok"], written)
        refused = self._run("m")
        self.assertIn("run m CarServiceUpdatable instead", refused["error"])
        again = self._run("m")
        self.assertTrue(again["ok"], again)


@unittest.skipUnless(POSIX and agent_shell.shutil.which("bash"), "needs bash")
class AdbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        adb = bin_dir / "adb"
        adb.write_text(textwrap.dedent('''\
            #!/bin/sh
            if [ "$1" = "devices" ]; then printf 'List of devices attached\\nemulator-5554\\tdevice\\n'; exit 0; fi
            if [ "$1" = "-s" ]; then shift 2; fi
            if [ "$1" = "shell" ]; then printf 'sdk_car_x86_64\\nuserdebug\\n35\\nautomotive\\n10\\n'; exit 0; fi
            if [ "$1" = "logcat" ] && [ "$2" = "-d" ]; then echo "E AndroidRuntime: FATAL EXCEPTION: main"; exit 0; fi
            if [ "$1" = "logcat" ]; then i=0; while true; do i=$((i+1)); echo "I CarService: tick $i"; sleep 0.05; done; fi
            echo "adb: more than one device/emulator" >&2; exit 1
        '''))
        adb.chmod(adb.stat().st_mode | stat.S_IEXEC)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        bash = Shell("bash", agent_shell.shutil.which("bash") or "bash", "bash")
        self.session = ShellSession(self.root, bash, available={"bash": bash}, env_extra={"PATH": self.path})
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"), session=self.session,
                               cache_dir=self.root / ".cache")

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def test_streaming_logcat_becomes_a_job_the_log_tool_can_read(self) -> None:
        result = execute("run_command", {"command": "adb logcat"}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertIn("started background job", result["output"])
        self.assertIn("logcat streams until stopped", result["output"])
        log_file = next(line for line in result["output"].splitlines() if "Its output goes to" in line)
        path = log_file.split("Its output goes to ", 1)[1].split(":", 1)[0]
        self.assertTrue(Path(path).is_file())
        self.assertTrue(str(path).startswith(str(self.root / ".cache" / "output")))
        execute("kill_command", {"job_id": "b1"}, self.ctx)

    def test_logcat_dump_runs_in_the_foreground(self) -> None:
        result = execute("run_command", {"command": "adb logcat -d"}, self.ctx)
        self.assertIn("FATAL EXCEPTION", result["output"])
        self.assertNotIn("background job", result["output"])

    def test_adb_failure_gets_a_hint(self) -> None:
        result = execute("run_command", {"command": "adb install app.apk"}, self.ctx)
        self.assertIn("pass -s <serial>", result["output"])

    def test_devices_line_for_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": self.path}):
            rows = agent_shell.android_devices()
        self.assertEqual(rows, ["emulator-5554 (sdk_car_x86_64, userdebug, API 35, automotive, current user 10)"])


class CommandShapeTests(unittest.TestCase):
    def test_logcat_streams(self) -> None:
        self.assertTrue(agent_build.is_logcat_stream("adb logcat"))
        self.assertTrue(agent_build.is_logcat_stream("adb -s emulator-5554 logcat -v threadtime ActivityManager:I *:S"))
        self.assertFalse(agent_build.is_logcat_stream("adb logcat -d"))
        self.assertFalse(agent_build.is_logcat_stream("adb logcat -t 200"))
        self.assertFalse(agent_build.is_logcat_stream("adb logcat -c && echo cleared"))
        self.assertFalse(agent_build.is_logcat_stream("adb devices"))

    def test_aosp_programs(self) -> None:
        self.assertEqual(agent_build.aosp_program("m CarService -j8"), "m")
        self.assertEqual(agent_build.aosp_program("atest CarServiceUnitTest"), "atest")
        self.assertEqual(agent_build.aosp_program("make -C x"), "")


if __name__ == "__main__":
    unittest.main()
