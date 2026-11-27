# SPDX-License-Identifier: Apache-2.0
"""The AOSP pack (Stage 9.2): manifest actions, pack loading, device safety, triage, test selection.

No real device and no real adb: device tests put a fake ``adb`` first on PATH, and the
triage tests run on the eval suite's own fixtures (tests/eval_tasks/aosp-*).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from critique_bot import eval_harness, packs, taint, tool_api
from critique_bot.extensions import manifest, session as ext_session
from critique_bot.packs.aosp import evidence, selinux, tests_for, tombstone, triage

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tests" / "eval_tasks"
PACK_TOOLS = ROOT / "src" / "critique_bot" / "packs" / "aosp" / "tools"


def _tool(name: str) -> manifest.ManifestTool:
    pack = packs.load("aosp")
    return manifest.load_manifest(PACK_TOOLS / f"{name}.json", scope="pack", handlers=pack.handlers)


def _ctx(workspace: Path, **extra):
    values = {"index_path": None, "state": None, "session": None, "cache_dir": None, "max_chars": 8000, "cancel": None}
    values.update(extra)
    return SimpleNamespace(workspace=Path(workspace), **values)


class _Isolated(unittest.TestCase):
    """A fresh CRIT_HOME and no active packs for each test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.tmp / "crit-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        packs.activate([])
        self.addCleanup(packs.activate, [])
        taint.reset()
        self.addCleanup(taint.reset)


# --------------------------------------------------------------------------- manifest actions


class ManifestActionTests(_Isolated):
    def test_pack_tools_load_and_pass_the_description_lint(self) -> None:
        for name in ("adb", "aosp_build", "triage"):
            tool = _tool(name)
            self.assertEqual(manifest.lint_description(tool.description), [], name)
            self.assertIn("action", tool.parameters["required"])
            self.assertEqual(sorted(tool.parameters["properties"]["action"]["enum"]), sorted(tool.actions))
        self.assertTrue(_tool("triage").spec().read_only)
        self.assertFalse(_tool("adb").spec().read_only)  # device actions

    def test_device_actions_ask_with_a_device_key_and_reads_do_not(self) -> None:
        adb = _tool("adb")
        ctx = _ctx(self.tmp)
        for action, args in (("install", {"apk": "out/Foo.apk"}), ("push", {"local": "a.txt", "remote": "/data/a"}),
                             ("reboot", {}), ("settings_put", {"namespace": "global", "key": "k", "value": "1"}),
                             ("uninstall", {"package": "com.x"}), ("flash", {"partition": "vendor_boot",
                                                                            "image": "out/vendor_boot.img"})):
            perm = adb.permission({"action": action, **args}, ctx)
            self.assertEqual(perm.kind, "command", action)
            self.assertTrue(perm.key.startswith(manifest.DEVICE_KEY_PREFIX), action)
            self.assertIn("changes the connected Android device", perm.risk)
        for action, args in (("devices", {}), ("getprop", {"name": "ro.build.type"}), ("dumpsys", {"service": "car_service"}),
                             ("logcat", {"filter": "CarService:V *:S"})):
            self.assertEqual(adb.permission({"action": action, **args}, ctx).kind, "read", action)
        self.assertEqual(adb.permission({"action": "pull", "remote": "/data/anr/x", "local": "anr/x"}, ctx).kind, "command")

    def test_shell_values_are_checked_before_anything_runs(self) -> None:
        adb = _tool("adb")
        bad = [
            {"action": "getprop", "name": "x; reboot"},
            {"action": "dumpsys", "service": "activity && reboot"},
            {"action": "dumpsys", "service": "--help"},
            {"action": "dumpsys"},
            {"action": "logcat", "filter": "x -c"},
            {"action": "pull", "remote": "/data/x", "local": "../../etc/passwd"},
            {"action": "install", "apk": "/tmp/evil.apk"},
            {"action": "settings_put", "namespace": "global", "key": "a b", "value": "1"},
            {"action": "format_disk"},
        ]
        for args in bad:
            _args, problems = adb.check(args)
            self.assertTrue(problems, args)
        result = adb.execute({"action": "getprop", "name": "$(reboot)"}, _ctx(self.tmp))
        self.assertFalse(result["ok"])
        self.assertIn("not allowed", result["error"])

    def test_action_names_are_forgiving_and_listed_when_wrong(self) -> None:
        adb = _tool("adb")
        args, problems = adb.check({"action": "Settings-Get", "namespace": "global", "key": "auto_time"})
        self.assertEqual(problems, [])
        self.assertEqual(args["action"], "settings_get")
        _args, problems = adb.check({"action": "frobnicate"})
        self.assertTrue(any("devices" in item for item in problems))

    def test_only_packs_may_name_a_python_handler(self) -> None:
        data = json.loads((PACK_TOOLS / "triage.json").read_text(encoding="utf-8"))
        with self.assertRaises(manifest.ManifestError):
            manifest.from_data(data, scope="user", handlers=packs.load("aosp").handlers)
        with self.assertRaises(manifest.ManifestError):
            manifest.from_data(data, scope="pack", handlers={})

    def test_a_user_manifest_can_use_actions_with_argv(self) -> None:
        tool = manifest.from_data({
            "name": "greet", "description": "Say hello. Not for anything else. Output: the greeting.",
            "read_only": True,
            "parameters": {"type": "object", "properties": {"who": {"type": "string"}}},
            "actions": {"hello": {"argv": [sys.executable, "-c", "print('hello')"]},
                        "loud": {"argv": [sys.executable, "-c", "print('HELLO')"], "read_only": False,
                                 "risk": "it shouts"}},
        }, scope="user")
        self.assertEqual(tool.permission({"action": "hello"}, None).kind, "read")
        self.assertEqual(tool.permission({"action": "loud"}, None).risk, "it shouts")
        result = tool.execute({"action": "hello"}, _ctx(self.tmp))
        self.assertTrue(result["ok"], result)
        self.assertIn("hello", result["output"])

    def test_action_enum_must_match_the_actions(self) -> None:
        with self.assertRaises(manifest.ManifestError):
            manifest.from_data({
                "name": "x1", "description": "d. Not for y. Output: z.",
                "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["a"]}}},
                "actions": {"a": {"argv": ["true"]}, "b": {"argv": ["true"]}},
            })


# --------------------------------------------------------------------------- pack activation


class PackActivationTests(_Isolated):
    def _workspace(self) -> Path:
        work = self.tmp / "ws"
        (work / ".crit" / "cache").mkdir(parents=True)
        return work

    def test_off_by_default_on_with_the_setting(self) -> None:
        work = self._workspace()
        plain = ext_session.load_session(work, {}, connect_mcp=False)
        self.assertEqual([t.name for t in plain.tools], [])
        self.assertNotIn("aosp-debugger", plain.agents)
        loaded = ext_session.load_session(work, {"packs": ["aosp"]}, connect_mcp=False)
        self.assertEqual(sorted(t.name for t in loaded.tools), ["adb", "aosp_build", "triage"])
        for name in ("aosp-debugger", "aosp-build-fixer", "selinux-helper"):
            self.assertIn(name, loaded.agents)
            self.assertEqual(loaded.agents[name].scope, "pack")
        self.assertEqual(packs.active(), ["aosp"])

    def test_unknown_pack_is_a_note(self) -> None:
        names, notes = packs.requested({"packs": ["aosp", "ios"]})
        self.assertEqual(names, ["aosp"])
        self.assertTrue(any("ios" in note for note in notes))

    def test_a_users_own_tool_replaces_the_pack_tool(self) -> None:
        work = self._workspace()
        tools = self.tmp / "crit-home" / "tools"
        tools.mkdir(parents=True)
        (tools / "adb.json").write_text(json.dumps({
            "name": "adb", "description": "My adb. Not for others. Output: text.", "read_only": True,
            "run": {"argv": ["adb", "devices"]}}), encoding="utf-8")
        loaded = ext_session.load_session(work, {"packs": ["aosp"]}, connect_mcp=False)
        adb = [t for t in loaded.tools if t.name == "adb"][0]
        self.assertEqual(adb.scope, "user")

    def test_an_aosp_tree_is_offered_the_pack_once(self) -> None:
        work = self._workspace()
        (work / "build").mkdir()
        (work / "build" / "envsetup.sh").write_text("# envsetup\n", encoding="utf-8")
        (work / "Android.bp").write_text("", encoding="utf-8")
        self.assertEqual(packs.detect(work), ["aosp"])
        first = ext_session.load_session(work, {}, connect_mcp=False)
        self.assertTrue(any('"packs": ["aosp"]' in note for note in first.notes))
        second = ext_session.load_session(work, {}, connect_mcp=False)
        self.assertFalse(any("packs" in note for note in second.notes))
        self.assertEqual(packs.offer(work, {"packs": []}, work / ".crit" / "cache"), "")

    def test_the_pack_sets_the_aosp_sandbox_preset_unless_one_is_named(self) -> None:
        self.assertEqual(packs.sandbox_value({"packs": ["aosp"]}), {"preset": "aosp"})
        self.assertEqual(packs.sandbox_value({"packs": ["aosp"], "sandbox": "read-only"}),
                         {"mode": "read-only", "preset": "aosp"})
        self.assertEqual(packs.sandbox_value({"packs": ["aosp"], "sandbox": {"preset": "android"}}), {"preset": "android"})
        self.assertIsNone(packs.sandbox_value({}))
        from critique_bot import sandbox

        self.assertEqual(sandbox.parse_settings(packs.sandbox_value({"packs": ["aosp"]})).preset, "aosp")

    def test_a_committed_settings_file_cannot_turn_a_pack_on(self) -> None:
        from critique_bot import project_home

        self.assertIn("packs", project_home.SECURITY_KEYS)


# --------------------------------------------------------------------------- device safety in the engine


class DeviceSafetyTests(_Isolated):
    def test_auto_mode_with_confirm_risky_off_still_asks_for_a_device_change(self) -> None:
        from rich.console import Console

        from critique_bot import agent, agent_ui
        from critique_bot.agent import run_agent_loop

        work = self.tmp / "ws"
        work.mkdir()
        loaded = ext_session.load_session(work, {"packs": ["aosp"]}, connect_mcp=False)
        old = tool_api.use_registry(loaded.registry(tool_api.builtin_registry()))
        self.addCleanup(tool_api.use_registry, old)
        agent.use_ui(agent_ui)
        self.addCleanup(agent.use_ui, None)
        agent_ui.reset()
        self.addCleanup(agent_ui.reset)
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100))
        seen: list[str] = []

        def approve(perm, *, risky=""):
            seen.append(risky)
            return ("no", "not now")

        class Scripted:
            last_detail = None

            def __init__(self) -> None:
                self.replies = ['<tool_call>\n{"tool": "adb", "arguments": {"action": "reboot"}}\n</tool_call>',
                                "COMPLETED"]
                self.sent: list[str] = []

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self.replies.pop(0)

        scripted = Scripted()
        from critique_bot.engine import ui as engine_ui

        with mock.patch.object(engine_ui, "_approve_prompt", approve):
            run_agent_loop(scripted, workspace=work, index_path=None, cache_dir=None, first_task="reboot it",
                           outcome=[], max_rounds=5, max_result_chars=8000, read_message=lambda: None,
                           emit=lambda _t: None, ask_user=lambda _q: None, approve_mode="auto",
                           settings={"confirm_risky": False})
        self.assertEqual(len(seen), 1, scripted.sent)
        self.assertIn("changes the connected Android device", seen[0])


# --------------------------------------------------------------------------- fake adb


_FAKE_ADB = r'''#!{python}
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_ADB_LOG"], "a") as log:
    log.write(" ".join(args) + " serial=" + os.environ.get("ANDROID_SERIAL", "") + "\n")
if args[:1] == ["devices"]:
    print("List of devices attached\nemulator-5554          device product:sdk_car_x86_64")
elif args[:1] == ["logcat"]:
    print("11-25 10:00:00.000  4242  4242 E AndroidRuntime: FATAL EXCEPTION: main")
    print("11-25 10:00:00.000  4242  4242 E AndroidRuntime: Process: com.android.car.hvac, PID: 4242")
    print("11-25 10:00:00.000  4242  4242 E AndroidRuntime: java.lang.IllegalStateException: fan level too high: 9")
    print("11-25 10:00:00.000  4242  4242 E AndroidRuntime: \tat com.android.car.hvac.HvacController.setFanLevel(HvacController.java:88)")
elif args[:2] == ["shell", "getprop"]:
    print("userdebug" if len(args) > 2 else "[ro.build.type]: [userdebug]")
else:
    print("ok " + " ".join(args))
'''


class FakeAdbTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "adb"
        fake.write_text(_FAKE_ADB.replace("{python}", sys.executable), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.calls = self.tmp / "adb-calls.txt"
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                                               "FAKE_ADB_LOG": str(self.calls)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = self.tmp / "ws"
        self.work.mkdir()
        if os.name == "nt":
            self.skipTest("the fake adb is a POSIX script")

    def _run(self, args: dict) -> dict:
        loaded = ext_session.load_session(self.work, {"packs": ["aosp"]}, connect_mcp=False)
        return loaded.extra_registry().execute("adb", args, _ctx(self.work))

    def test_read_only_queries_run_the_fake_and_taint_the_session_as_a_device_log(self) -> None:
        result = self._run({"action": "getprop", "name": "ro.build.type", "serial": "emulator-5554"})
        self.assertTrue(result["ok"], result)
        self.assertIn("userdebug", result["output"])
        self.assertEqual(result["taint"], "device:adb")
        self.assertIn("<untrusted", result["output"])
        self.assertIn("shell getprop ro.build.type serial=emulator-5554", self.calls.read_text(encoding="utf-8"))
        self.assertTrue(taint.tainted())
        self.assertTrue(any(source.startswith("device_log") for source in taint.state().sources))

    def test_devices_is_not_a_taint_source(self) -> None:
        result = self._run({"action": "devices"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(taint.tainted())

    def test_logcat_is_saved_in_the_workspace_and_summarized(self) -> None:
        result = self._run({"action": "logcat", "lines": 100})
        self.assertTrue(result["ok"], result)
        self.assertIn("saved", result["output"])
        self.assertIn("app crash", result["output"])
        saved = list((self.work / ".crit" / "cache" / "device").glob("logcat-*.txt"))
        self.assertEqual(len(saved), 1)
        self.assertIn("-t 100", self.calls.read_text(encoding="utf-8"))
        self.assertTrue(taint.tainted())


# --------------------------------------------------------------------------- triage on the eval fixtures


def _prepared(name: str, scratch: Path) -> Path:
    task = next(t for t in eval_harness.load_tasks(TASKS) if t.name == name)
    if eval_harness.missing(task.spec):
        raise unittest.SkipTest(f"{name} needs {eval_harness.missing(task.spec)}")
    work, error = eval_harness.prepare(task, scratch)
    if error:
        raise AssertionError(error)
    return work


class TriageTests(_Isolated):
    def _triage(self, task: str, action: str, args: dict, **ctx_extra) -> str:
        work = _prepared(task, self.tmp / task)
        ok, text = triage.run(action, args, _ctx(work, **ctx_extra))
        self.assertTrue(ok, text)
        return text

    def test_crash_finds_the_root_cause_frame_not_the_framework_top(self) -> None:
        text = self._triage("aosp-crash-root-cause", "crash", {"path": "logcat.txt"})
        self.assertIn("root cause: java.lang.IllegalStateException", text)
        self.assertIn("packages/apps/Hvac/src/com/android/car/hvac/HvacController.java:88", text)
        self.assertIn("CarHvacApp (android_app)", text)

    def test_crash_maps_a_filegroup_file_to_the_app_module(self) -> None:
        text = self._triage("aosp-crash-module", "crash", {"path": "logcat.txt"})
        self.assertIn("CarPowerManagementService.java:412", text)
        self.assertIn("CarServiceUpdatable (android_app)", text)
        self.assertIn("null: mPowerComponentHandler", text)

    def test_crash_ranks_the_commit_that_changed_the_null_field_first(self) -> None:
        text = self._triage("aosp-crash-suspect-commit", "crash", {"path": "logcat.txt"})
        first = [line for line in text.splitlines() if line.strip().startswith("1. ")][0]
        self.assertIn("power: skip garage mode setup when shutdown is pending", first)

    def test_crash_outside_the_tree_is_refused_not_guessed(self) -> None:
        text = self._triage("aosp-crash-not-in-tree", "crash", {"path": "logcat.txt"})
        self.assertIn("NO CANDIDATE", text)
        self.assertIn("refuse to guess", text)
        self.assertNotIn("packages/apps/Maps", text)

    def test_anr_names_the_lock_holder(self) -> None:
        text = self._triage("aosp-anr-lock", "anr", {"path": "logcat.txt",
                                                     "traces": "anr/anr_2026-11-25-10-05-00-123"})
        self.assertIn('held by thread 18 "TunerWorker"', text)
        self.assertIn("TunerSession.java:95 in TunerSession.scanBand", text)

    def test_tombstone_symbolizes_with_the_trees_symbols(self) -> None:
        if not shutil.which("addr2line"):
            self.skipTest("needs addr2line")
        text = self._triage("aosp-tombstone-symbolize", "tombstone", {"path": "tombstone_03"})
        self.assertIn("symbolized 1 frame", text)
        self.assertIn("VhalClient.cpp:26", text)
        self.assertIn("libvhalclient (cc_library)", text)

    def test_tombstone_without_symbols_says_so_and_uses_the_abort_message(self) -> None:
        text = self._triage("aosp-tombstone-abort", "tombstone", {"path": "logcat.txt"})
        self.assertIn("unsymbolized", text)
        self.assertIn("CarPowerPolicyServer.cpp:144", text)
        self.assertIn("carpowerpolicyd (cc_binary)", text)

    def test_crash_on_a_native_crash_falls_through_to_the_tombstone_pipeline(self) -> None:
        text = self._triage("aosp-tombstone-abort", "crash", {"path": "logcat.txt"})
        self.assertIn("TRIAGE tombstone", text)

    def test_selinux_picks_the_enforced_denial_of_the_dying_service(self) -> None:
        text = self._triage("aosp-selinux-sysfs", "selinux", {"path": "dmesg.txt"})
        self.assertIn("minimal rule: allow vendor_cabin_sensor sysfs_cabin_temp:file { open read };", text)
        self.assertIn("edit: sepolicy/vendor/cabin_sensor.te", text)

    def test_selinux_on_a_generic_label_proposes_a_new_type(self) -> None:
        text = self._triage("aosp-selinux-generic-label", "selinux", {"path": "dmesg.txt"})
        self.assertIn("do NOT add it", text)
        self.assertIn("genfscon sysfs /devices/platform/fan-hwmon u:object_r:sysfs_fan_hwmon:s0", text)

    def test_build_missing_package_names_the_module_to_add(self) -> None:
        text = self._triage("aosp-build-missing-dep", "build", {"path": "build.log"})
        self.assertIn('add "car-hvac-common" to static_libs of CarHvacApp', text)

    def test_build_undefined_module_finds_the_rename_commit(self) -> None:
        text = self._triage("aosp-build-undefined-module", "build", {"path": "build.log"})
        first = [line for line in text.splitlines() if line.strip().startswith("1. ")][0]
        self.assertIn("rename car-radio-common to car-radio-lib", first)
        self.assertIn('Soong suggests: "car-radio-lib"', text)

    def test_build_cpp_error_maps_to_file_and_module(self) -> None:
        text = self._triage("aosp-build-cpp-member", "build", {"path": "build.log"})
        self.assertIn("hardware/google/car/fan/fan_hal.cpp:42", text)
        self.assertIn("missing symbol: setLevel", text)
        self.assertIn("libcarfanhal (cc_library_shared)", text)

    def test_big_files_go_through_log_tools_with_the_same_result(self) -> None:
        with mock.patch.object(triage, "WHOLE_FILE_BYTES", 1000):
            crash = self._triage("aosp-crash-root-cause", "crash", {"path": "logcat.txt"})
            denial = self._triage("aosp-selinux-sysfs", "selinux", {"path": "dmesg.txt"})
        self.assertIn("HvacController.java:88", crash)
        self.assertIn("allow vendor_cabin_sensor sysfs_cabin_temp:file { open read };", denial)

    def test_the_index_resolves_frames_when_there_is_one(self) -> None:
        from critique_bot.indexing import engine

        work = _prepared("aosp-crash-root-cause", self.tmp / "indexed")
        db = self.tmp / "index.db"
        engine._build_main(work, db)
        ok, text = triage.run("crash", {"path": "logcat.txt"}, _ctx(work, index_path=db))
        self.assertTrue(ok, text)
        self.assertIn("HvacController.java:88 in HvacController.setFanLevel  [index]", text)

    def test_pasted_stack_and_missing_input(self) -> None:
        ok, text = triage.run("crash", {"text": "java.lang.IllegalStateException: boom here now\n"
                                                "  at com.vendor.x.Y.z(Y.java:3)"}, _ctx(self.tmp))
        self.assertTrue(ok)
        self.assertIn("NO CANDIDATE", text)
        ok, text = triage.run("crash", {}, _ctx(self.tmp))
        self.assertFalse(ok)
        self.assertIn("pass path", text)
        ok, text = triage.run("anr", {"text": "nothing here"}, _ctx(self.tmp))
        self.assertTrue(ok)
        self.assertIn("verdict: no anr found", text)

    def test_triage_reads_outside_the_workspace_only_when_asked(self) -> None:
        tool = _tool("triage")
        ctx = tool_api.ToolContext(workspace=self.tmp)
        self.assertEqual(tool.permission({"action": "crash", "path": "logcat.txt"}, ctx).kind, "read")
        self.assertEqual(tool.permission({"action": "crash", "path": "/etc/hostname"}, ctx).kind, "outside")

    def test_device_log_triage_taints_and_build_triage_does_not(self) -> None:
        work = _prepared("aosp-crash-root-cause", self.tmp / "taint")
        loaded = ext_session.load_session(work, {"packs": ["aosp"]}, connect_mcp=False)
        registry = loaded.extra_registry()
        (work / "build.log").write_text("x.java:1: error: boom\n", encoding="utf-8")
        result = registry.execute("triage", {"action": "build", "path": "build.log"}, _ctx(work))
        self.assertTrue(result["ok"], result)
        self.assertFalse(taint.tainted())
        result = registry.execute("triage", {"action": "crash", "path": "logcat.txt"}, _ctx(work))
        self.assertTrue(result["ok"], result)
        self.assertTrue(taint.tainted())


class SelinuxUnitTests(unittest.TestCase):
    def test_rule_is_minimal_and_uses_self(self) -> None:
        line = ('avc: denied { read write } for pid=1 comm="x" scontext=u:r:foo:s0 tcontext=u:r:foo:s0 '
                'tclass=unix_dgram_socket permissive=0')
        groups = selinux.group([selinux.parse_line(1, line)])
        self.assertEqual(groups[0].rule(), "allow foo self:unix_dgram_socket { read write };")

    def test_granted_and_garbage_lines_are_ignored(self) -> None:
        self.assertIsNone(selinux.parse_line(1, "avc: granted { read } for scontext=u:r:a:s0 tcontext=u:r:b:s0 tclass=file"))
        self.assertIsNone(selinux.parse_line(1, "avc: denied { * } for scontext=u:r:a:s0 tcontext=u:r:b:s0 tclass=file"))


class TombstoneUnitTests(unittest.TestCase):
    def test_frames_with_and_without_function_names(self) -> None:
        stone = tombstone.parse(list(enumerate([
            "pid: 1, tid: 2, name: x  >>> /system/bin/x <<<",
            "signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0",
            "backtrace:",
            "      #00 pc 0000000000001000  /system/lib64/libc.so (abort+10) (BuildId: aa)",
            "      #01 pc 0000000000002000  /vendor/lib64/libfoo.so (BuildId: bb)",
            "      #02 pc 0000000000003000  /vendor/lib64/libfoo.so (ns::Foo::bar(int) const+52)",
        ], start=1)))
        self.assertEqual([f.func for f in stone.frames], ["abort+10", "", "ns::Foo::bar(int) const+52"])
        self.assertEqual(tombstone.failing_frame(stone).number, 1)
        self.assertEqual(tombstone.short_function("ns::Foo::bar(int) const+52"), "Foo::bar")


# --------------------------------------------------------------------------- tests_for and aosp_build


class TestsForTests(_Isolated):
    def test_test_mapping_and_instrumentation_tests(self) -> None:
        work = _prepared("aosp-tests-for-change", self.tmp / "t")
        picked = tests_for.select(work, ["packages/services/Car/service/src/com/android/car/power/CarPowerManagementService.java"])
        names = [pick.name for pick in picked.tests]
        for name in ("CarServicePowerUnitTest", "CarServiceTest", "CarServiceHostUnitTest", "CarServiceStressTest"):
            self.assertIn(name, names)
        self.assertNotIn("CarLibUnitTest", names)
        self.assertLess(names.index("CarServicePowerUnitTest"), names.index("CarServiceStressTest"))

    def test_no_tests_says_so(self) -> None:
        (self.tmp / "a").mkdir()
        (self.tmp / "a" / "Android.bp").write_text('java_library { name: "lib-a", srcs: ["*.java"] }\n', encoding="utf-8")
        (self.tmp / "a" / "A.java").write_text("class A {}\n", encoding="utf-8")
        text = tests_for.render(tests_for.select(self.tmp, ["a/A.java"]))
        self.assertIn("no test found", text)

    def test_aosp_build_actions(self) -> None:
        work = _prepared("aosp-tests-for-change", self.tmp / "b")
        tool = _tool("aosp_build")
        ctx = tool_api.ToolContext(workspace=work)
        result = tool.execute({"action": "tests_for", "paths": [
            "packages/services/Car/service/src/com/android/car/power/CarPowerManagementService.java"]}, ctx)
        self.assertTrue(result["ok"], result)
        self.assertIn("atest CarServiceHostUnitTest CarServicePowerUnitTest CarServiceTest", result["output"])
        result = tool.execute({"action": "lunch"}, ctx)
        self.assertTrue(result["ok"])
        self.assertIn("pass target", result["output"])
        result = tool.execute({"action": "m"}, ctx)
        self.assertFalse(result["ok"])
        self.assertIn("m alone builds the whole platform", result["error"])
        ran: list[dict] = []

        def fake_execute(name, arguments, _ctx):
            ran.append({"tool": name, **arguments})
            return {"tool": name, "ok": True, "output": "built"}

        target = work / "packages/services/Car/service/src/com/android/car/power/CarPowerManagementService.java"
        target.write_text(target.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")
        from critique_bot import agent_tools

        with mock.patch.object(agent_tools, "execute", fake_execute):
            self.assertTrue(tool.execute({"action": "m"}, ctx)["ok"])
            self.assertTrue(tool.execute({"action": "m", "modules": ["CarServiceUpdatable", "x;rm"]}, ctx)["ok"] is False)
            self.assertTrue(tool.execute({"action": "atest"}, ctx)["ok"])
        self.assertEqual(ran[0], {"tool": "run_command", "command": "m CarServiceUpdatable"})
        self.assertEqual(ran[1]["command"], "atest CarServiceHostUnitTest CarServicePowerUnitTest CarServiceTest")
        perm = tool.permission({"action": "m", "modules": ["CarServiceUpdatable"]}, ctx)
        self.assertEqual(perm.kind, "command")
        self.assertEqual(perm.detail, "m CarServiceUpdatable")
        self.assertEqual(tool.permission({"action": "tests_for"}, ctx).kind, "read")


class EvidenceTests(unittest.TestCase):
    def test_longest_fragment_drops_numbers_and_ids(self) -> None:
        self.assertEqual(evidence._longest_fragment("fan level too high: 12 (id 0x3f)"), "fan level too high")


class TaintObserveTests(_Isolated):
    def test_a_flagged_result_marks_the_session(self) -> None:
        taint.observe("anything", {}, {"ok": True, "output": "x", "taint": "device:adb"})
        self.assertTrue(any(source.startswith("device_log") for source in taint.state().sources))
        taint.reset()
        taint.observe("anything", {}, {"ok": True, "output": "x", "taint": "tool:mine"})
        self.assertTrue(any(source.startswith("tool_output") for source in taint.state().sources))


if __name__ == "__main__":
    unittest.main()
