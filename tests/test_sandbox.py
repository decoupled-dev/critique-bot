# SPDX-License-Identifier: Apache-2.0
"""The OS sandbox: settings, policies, the backends' generated arguments, and real escape tests.

The escape tests run for real wherever bubblewrap works (this machine, Linux CI) and are
skipped elsewhere. Every one of them must fail closed: a write outside the workspace, a
read of a secret, a direct connection, a DNS lookup, a request to a host the allow-list
does not name.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from critique_bot import agent_shell, agent_tools, audit, egress, egress_proxy, sandbox, taint
from critique_bot.sandbox import linux, macos, windows


def _outside_tmp() -> Path:
    """A folder outside /tmp (the sandbox's private /tmp would hide anything under it)."""
    for base in ("/var/tmp", str(Path(__file__).resolve().parents[1] / ".crit-test-tmp")):
        try:
            os.makedirs(base, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="crit-sbx-", dir=base))
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="crit-sbx-"))


BWRAP = sys.platform.startswith("linux") and sandbox.availability().ok


class SettingsTests(unittest.TestCase):
    def test_default_is_workspace_write(self) -> None:
        self.assertEqual(sandbox.parse_settings(None).mode, sandbox.WORKSPACE_WRITE)
        self.assertFalse(sandbox.parse_settings(None).explicit)

    def test_mode_names_and_aliases(self) -> None:
        for raw, mode in [("read-only", "read-only"), ("readonly", "read-only"), ("workspace", "workspace-write"),
                          ("full", "full"), ("off", "full"), ("WORKSPACE_WRITE", "workspace-write")]:
            self.assertEqual(sandbox.parse_settings(raw).mode, mode, raw)

    def test_object_form(self) -> None:
        found = sandbox.parse_settings({"mode": "read-only", "writable": ["~/.cache/x"], "preset": "aosp",
                                        "env_files": True, "ports": [5037, 8080]})
        self.assertEqual(found.mode, "read-only")
        self.assertEqual(found.writable, ("~/.cache/x",))
        self.assertEqual(found.preset, "aosp")
        self.assertTrue(found.env_files)
        self.assertEqual(found.ports, (5037, 8080))

    def test_config_default_then_settings(self) -> None:
        found = sandbox.parse_settings({"writable": ["build"]}, default={"mode": "read-only", "preset": "aosp"})
        self.assertEqual((found.mode, found.preset, found.writable), ("read-only", "aosp", ("build",)))
        self.assertEqual(sandbox.parse_settings(None, default="full").mode, "full")

    def test_bad_values_say_what_is_wrong(self) -> None:
        for bad, words in [("sometimes", "mode must be one of"), ({"mode": "x"}, "mode must be one of"),
                           ({"writable": "a", "nope": 1}, "unknown keys: nope"), ({"preset": "nasa"}, "preset"),
                           ({"ports": ["5037"]}, "port numbers"), ({"env_files": "yes"}, "true or false"), (3, "mode name"),
                           ({"masked": [1]}, "list of paths"), ({"unmask": {}}, "list of paths")]:
            with self.assertRaises(ValueError) as caught:
                sandbox.parse_settings(bad)
            self.assertIn(words, str(caught.exception))

    def test_a_committed_settings_file_cannot_set_it(self) -> None:
        from critique_bot.project_home import SECURITY_KEYS

        self.assertIn("sandbox", SECURITY_KEYS)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _outside_tmp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / "home"
        self.ws = self.base / "ws"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_rsa").write_text("PRIVATE", encoding="utf-8")
        (self.home / ".netrc").write_text("machine x password y", encoding="utf-8")
        (self.home / ".gradle").mkdir()
        (self.ws / ".crit").mkdir(parents=True)
        (self.ws / ".crit" / "settings.json").write_text("{}", encoding="utf-8")
        (self.ws / ".git" / "hooks").mkdir(parents=True)
        (self.ws / ".git" / "config").write_text("", encoding="utf-8")
        (self.ws / ".env").write_text("TOKEN=x", encoding="utf-8")
        (self.ws / "app").mkdir()
        (self.ws / "app" / ".env.local").write_text("TOKEN=y", encoding="utf-8")

    def policy(self, value: object = None, **kwargs: object) -> sandbox.Policy:
        return sandbox.build_policy(sandbox.parse_settings(value), self.ws, home=self.home, make_caches=False, **kwargs)

    def test_secrets_are_masked(self) -> None:
        policy = self.policy()
        masked = {str(item) for item in policy.masked}
        self.assertIn(str(self.home / ".ssh"), masked)
        self.assertIn(str(self.home / ".netrc"), masked)
        self.assertIn(str((self.ws / ".env").resolve()), masked)
        self.assertIn(str((self.ws / "app" / ".env.local").resolve()), masked)

    def test_env_files_can_stay_readable(self) -> None:
        masked = {str(item) for item in self.policy({"env_files": True}).masked}
        self.assertNotIn(str((self.ws / ".env").resolve()), masked)

    def test_crit_files_and_git_hooks_are_read_only(self) -> None:
        policy = self.policy()
        protected = {str(item) for item in policy.protected}
        for name in (".crit/settings.json", ".git/config", ".git/hooks"):
            self.assertIn(str((self.ws / name).resolve()), protected)
        self.assertFalse(policy.can_write(self.ws / ".crit" / "settings.json"))
        self.assertFalse(policy.can_write(self.ws / ".git" / "hooks" / "pre-commit"))
        self.assertTrue(policy.can_write(self.ws / "app" / "main.py"))

    def test_caches_are_writable_only_in_workspace_write(self) -> None:
        (self.home / ".gradle" / "caches").mkdir()
        self.assertIn((self.home / ".gradle" / "caches").resolve(), self.policy().writable)
        self.assertNotIn((self.home / ".m2").resolve(), self.policy().writable, "a missing cache is not invented")
        read_only = self.policy("read-only")
        self.assertEqual(read_only.writable, ())
        self.assertFalse(read_only.can_write(self.ws / "app" / "x"))
        self.assertFalse(self.policy().can_write(self.home / "x"))

    def test_a_git_worktree_can_commit_but_not_change_config_or_hooks(self) -> None:
        main = self.base / "main" / ".git"
        (main / "worktrees" / "wt").mkdir(parents=True)
        (main / "hooks").mkdir()
        (main / "config").write_text("", encoding="utf-8")
        (main / "worktrees" / "wt" / "commondir").write_text("../..\n", encoding="utf-8")
        shutil.rmtree(self.ws / ".git")
        (self.ws / ".git").write_text(f"gitdir: {main / 'worktrees' / 'wt'}\n", encoding="utf-8")
        policy = self.policy()
        self.assertTrue(policy.can_write(main / "objects" / "ab" / "cd"))
        self.assertTrue(policy.can_write(main / "worktrees" / "wt" / "index"))
        self.assertFalse(policy.can_write(main / "config"))
        self.assertFalse(policy.can_write(main / "hooks" / "pre-commit"))
        self.assertFalse(self.policy("read-only").can_write(main / "objects" / "x"))

    def test_aosp_preset_adds_out_and_build_caches(self) -> None:
        (self.ws / "out").mkdir()
        (self.home / ".ccache").mkdir()
        policy = sandbox.build_policy(sandbox.parse_settings({"preset": "aosp"}), self.ws, home=self.home, proxy_port=1)
        writable = {str(item) for item in policy.writable}
        self.assertIn(str((self.ws / "out").resolve()), writable)
        self.assertIn(str((self.home / ".ccache").resolve()), writable)
        self.assertIn(str((self.home / ".cache" / "bazel").resolve()), writable, "made on first use")
        self.assertIn(5037, policy.ports)

    def test_credential_files_are_masked_and_can_be_unmasked(self) -> None:
        # SB-1: package-registry and forge tokens; "unmask" keeps one readable for a build that needs it.
        for rel in (".npmrc", ".cargo/credentials.toml", ".config/gh/hosts.yml", ".s3cfg", ".m2/settings.xml"):
            (self.home / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.home / rel).write_text("FAKE", encoding="utf-8")
        masked = set(self.policy().masked)
        for rel in (".npmrc", ".cargo/credentials.toml", ".config/gh", ".s3cfg", ".m2/settings.xml"):
            self.assertIn((self.home / rel).resolve(), masked, rel)
        loose = set(self.policy({"unmask": ["~/.m2/settings.xml"], "masked": ["~/.gradle/gradle.properties"]}).masked)
        self.assertNotIn((self.home / ".m2" / "settings.xml").resolve(), loose)
        (self.home / ".gradle" / "gradle.properties").write_text("signingPassword=x", encoding="utf-8")
        loose = set(self.policy({"masked": ["~/.gradle/gradle.properties"]}).masked)
        self.assertIn((self.home / ".gradle" / "gradle.properties").resolve(), loose)

    def test_the_credential_list_is_shared_with_the_core_layer(self) -> None:
        from critique_bot import secret_paths

        self.assertIs(sandbox.SECRET_HOME_PATHS, secret_paths.HOME_CREDENTIALS)

    def test_cache_auto_run_files_are_read_only_and_cannot_be_created(self) -> None:
        # SB-2: the cache folders below ~/.gradle and ~/.m2 are writable, its auto-run files are not.
        (self.home / ".gradle" / "init.d").mkdir()
        (self.home / ".gradle" / "custom-cache").mkdir()
        policy = sandbox.build_policy(sandbox.Settings(), self.ws, home=self.home, make_caches=True)
        for rel in (".gradle/caches/modules-2/x.jar", ".gradle/wrapper/dists/x", ".gradle/custom-cache/x",
                    ".m2/repository/org/x.jar"):
            self.assertTrue(policy.can_write(self.home / rel), rel)
        for rel in (".gradle/init.gradle", ".gradle/init.gradle.kts", ".gradle/init.d/x.gradle",
                    ".gradle/gradle.properties", ".gradle/new-top-level", ".m2/settings.xml", ".m2/toolchains.xml"):
            self.assertFalse(policy.can_write(self.home / rel), rel)
        self.assertNotIn((self.home / ".gradle").resolve(), policy.writable)
        self.assertFalse((self.home / ".gradle" / "init.gradle").exists(), "no placeholder is left in ~/.gradle")
        self.assertIsNone(sandbox.escape_target(str(self.home / ".gradle" / "init.gradle"), policy),
                          "the cache folder is never offered as an escape")
        # Listed in "writable" by hand, or allowed as an escape: still split the same way.
        again = sandbox.build_policy(sandbox.parse_settings({"writable": ["~/.gradle"]}), self.ws, home=self.home,
                                     make_caches=False, extra_writable=(self.home / ".gradle",))
        self.assertFalse(again.can_write(self.home / ".gradle" / "init.gradle"))
        self.assertTrue(again.can_write(self.home / ".gradle" / "caches" / "x"))

    def test_ccache_config_is_made_empty_and_kept_read_only(self) -> None:
        (self.home / ".ccache").mkdir()
        policy = sandbox.build_policy(sandbox.parse_settings({"preset": "aosp"}), self.ws, home=self.home)
        conf = self.home / ".ccache" / "ccache.conf"
        self.assertEqual(conf.read_text(encoding="utf-8"), "", "an empty config is ccache's defaults")
        self.assertFalse(policy.can_write(conf))
        self.assertTrue(policy.can_write(self.home / ".ccache" / "0" / "x"))
        args = linux.bwrap_args(policy, cwd=self.ws)
        self.assertIn(["--ro-bind", str(conf.resolve()), str(conf.resolve())],
                      [args[i:i + 3] for i in range(len(args) - 2)])

    def test_a_masked_file_that_is_also_protected_stays_masked(self) -> None:
        (self.home / ".m2").mkdir()
        (self.home / ".m2" / "settings.xml").write_text("<password>FAKE</password>", encoding="utf-8")
        policy = self.policy()
        real = str((self.home / ".m2" / "settings.xml").resolve())
        args = linux.bwrap_args(policy, cwd=self.ws)
        triples = [args[i:i + 3] for i in range(len(args) - 2)]
        self.assertIn(["--ro-bind", "/dev/null", real], triples)
        self.assertNotIn(["--ro-bind", real, real], triples)

    def test_env_files_at_any_depth_are_masked(self) -> None:
        # SB-3: no depth limit; a scan that stops early says so.
        deep = self.ws / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / ".env").write_text("X=1", encoding="utf-8")
        (deep / "prod.env").write_text("X=1", encoding="utf-8")
        policy = self.policy()
        self.assertIn((deep / ".env").resolve(), policy.masked)
        self.assertIn((deep / "prod.env").resolve(), policy.masked)
        self.assertTrue(policy.env_scan_complete)
        found, complete = sandbox.env_files(self.ws, limit_dirs=2)
        self.assertFalse(complete)
        self.assertIn(self.ws / ".env", found, "breadth first: the top level is done before it stops")

    def test_full_mode_has_nothing_to_wrap(self) -> None:
        policy = self.policy("full")
        self.assertFalse(policy.sandboxed)
        argv, env = sandbox.wrap(["ls"], policy, {"A": "1"}, cwd=self.ws, platform_name="linux")
        self.assertEqual(argv, ["ls"])
        self.assertEqual(env, {"A": "1"})

    def test_sandbox_env_points_at_the_proxy_and_drops_agent_sockets(self) -> None:
        policy = self.policy(proxy_port=4321, proxy_socket="/x/p.sock", network=True)
        env = sandbox.sandbox_env({"SSH_AUTH_SOCK": "/run/x", "PATH": "/bin"}, policy)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:4321")
        self.assertIn("-Dhttps.proxyPort=4321", env["JAVA_TOOL_OPTIONS"])
        self.assertEqual(env["TMPDIR"], "/tmp")
        offline = sandbox.sandbox_env({"HTTPS_PROXY": "http://127.0.0.1:9"}, policy)
        self.assertEqual(offline["HTTPS_PROXY"], "http://127.0.0.1:9", "offline mode keeps its closed proxy")


class LinuxArgsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _outside_tmp()
        self.addCleanup(shutil.rmtree, self.base, True)
        (self.base / "home" / ".ssh").mkdir(parents=True)
        (self.base / "ws").mkdir()
        (self.base / "ws" / ".env").write_text("x", encoding="utf-8")

    def test_order_root_ro_then_tmp_then_binds_then_masks(self) -> None:
        policy = sandbox.build_policy(sandbox.Settings(), self.base / "ws", home=self.base / "home", make_caches=False)
        args = linux.bwrap_args(policy, cwd=self.base / "ws")
        joined = " ".join(args)
        self.assertTrue(joined.startswith("--ro-bind / / --dev /dev --proc /proc --tmpfs /tmp"))
        ws = str((self.base / "ws").resolve())
        self.assertLess(args.index("--tmpfs"), args.index(ws))
        self.assertIn(f"--bind {ws} {ws}", joined)
        self.assertIn(f"--tmpfs {(self.base / 'home' / '.ssh').resolve()}", joined)
        self.assertIn(f"--ro-bind /dev/null {(self.base / 'ws' / '.env').resolve()}", joined)
        self.assertIn(f"--remount-ro {(self.base / 'home' / '.ssh').resolve()}", joined)
        for flag in ("--unshare-all", "--die-with-parent", "--new-session"):
            self.assertIn(flag, args)
        self.assertNotIn("--share-net", args)

    def test_read_only_binds_the_workspace_read_only(self) -> None:
        policy = sandbox.build_policy(sandbox.Settings(mode="read-only"), self.base / "ws", home=self.base / "home")
        joined = " ".join(linux.bwrap_args(policy, cwd=self.base / "ws"))
        ws = str((self.base / "ws").resolve())
        self.assertIn(f"--ro-bind {ws} {ws}", joined)
        self.assertNotIn(f"--bind {ws} {ws}", joined)

    def test_the_bridge_runs_first_only_with_a_proxy(self) -> None:
        bare = sandbox.build_policy(sandbox.Settings(), self.base / "ws", home=self.base / "home", make_caches=False)
        self.assertEqual(linux.wrap(["make"], bare, {}, cwd=self.base, bwrap="bwrap")[-2:], ["--", "make"])
        proxied = sandbox.build_policy(sandbox.Settings(), self.base / "ws", home=self.base / "home", make_caches=False,
                                       proxy_socket="/s/p.sock", proxy_port=3128, network=True)
        argv = linux.wrap(["make"], proxied, {}, cwd=self.base, bwrap="bwrap")
        start = argv.index("--") + 1
        self.assertEqual(argv[start:start + 3], [sys.executable, "-I", "-S"])
        self.assertEqual(argv[start + 4:start + 7], ["/s/p.sock", "3128", "5037"])
        self.assertEqual(argv[-1], "make")

    def test_probe_explains_a_missing_bwrap(self) -> None:
        with mock.patch.object(linux, "bwrap_path", return_value=None):
            found = linux.probe()
        self.assertFalse(found.ok)
        self.assertIn("not installed", found.problem)
        self.assertIn("apt install bubblewrap", found.fix)

    def test_probe_explains_disabled_user_namespaces(self) -> None:
        def runner(argv, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="bwrap: setting up uid map: Permission denied\n")

        found = linux.probe(bwrap="/usr/bin/bwrap", runner=runner)
        self.assertFalse(found.ok)
        self.assertIn("uid map", found.problem)
        self.assertIn("apparmor_restrict_unprivileged_userns", found.fix)
        self.assertIn("unprivileged_userns_clone", found.fix)

    def test_probe_ok(self) -> None:
        def runner(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="bubblewrap 0.9.0\n", stderr="")

        found = linux.probe(bwrap="/usr/bin/bwrap", runner=runner)
        self.assertTrue(found.ok)
        self.assertIn("bubblewrap 0.9.0", found.details[0])


class MacProfileTests(unittest.TestCase):
    def test_profile_denies_by_default_and_allows_the_workspace(self) -> None:
        policy = sandbox.Policy(
            mode="workspace-write", workspace=Path("/Users/me/proj"), home=Path("/Users/me"),
            writable=(Path("/Users/me/.gradle"),), private=(Path("/private/var/folders/x/crit-shell-1"),),
            masked=(Path("/Users/me/.ssh-file"),), protected=(Path("/Users/me/proj/.crit/settings.json"),),
            network="proxy", proxy_port=3128, ports=(5037,),
        )
        text = macos.profile(policy, temp_roots=["/private/tmp"])
        lines = text.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(deny default)"])
        self.assertIn("(allow file-read*)", lines)
        write = next(line for line in lines if line.startswith("(allow file-write*"))
        for root in ("/Users/me/proj", "/Users/me/.gradle", "/private/tmp", "/private/var/folders/x/crit-shell-1"):
            self.assertIn(f'(subpath "{root}")', write)
        self.assertIn('(deny file-read* file-write* (literal "/Users/me/.ssh-file"))', lines)
        self.assertIn('(deny file-write* (literal "/Users/me/proj/.crit/settings.json"))', lines)
        self.assertIn('(allow network-outbound (remote ip "localhost:3128"))', lines)
        self.assertIn('(allow network-outbound (remote ip "localhost:5037"))', lines)
        self.assertFalse(any("network-outbound" in line and "*" in line for line in lines), "no outbound wildcard")
        # deny rules come after the allows (later rules win in SBPL)
        self.assertGreater(lines.index('(deny file-read* file-write* (literal "/Users/me/.ssh-file"))'), lines.index(write))

    def test_read_only_profile_writes_only_temp(self) -> None:
        policy = sandbox.Policy(mode="read-only", workspace=Path("/w"), home=Path("/h"))
        write = next(line for line in macos.profile(policy, temp_roots=["/private/tmp"]).splitlines()
                     if line.startswith("(allow file-write*"))
        self.assertNotIn('"/w"', write)
        self.assertNotIn("network-outbound", macos.profile(policy, temp_roots=[]))

    def test_quoting(self) -> None:
        self.assertEqual(macos.quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_wrap(self) -> None:
        policy = sandbox.Policy(mode="read-only", workspace=Path("/w"), home=Path("/h"))
        argv = macos.wrap(["ls", "-l"], policy, cwd=Path("/w"))
        self.assertEqual(argv[:2], ["/usr/bin/sandbox-exec", "-p"])
        self.assertEqual(argv[-2:], ["ls", "-l"])


class WindowsTests(unittest.TestCase):
    def test_reports_best_effort(self) -> None:
        found = windows.probe()
        self.assertFalse(found.ok)
        self.assertIn("permission prompts", found.fix)
        argv, _env = sandbox.wrap(["cmd"], sandbox.Policy(mode="workspace-write", workspace=Path("C:/w"), home=Path("C:/h")),
                                  {}, cwd="C:/w", platform_name="win32")
        self.assertEqual(argv, ["cmd"])


class RunTests(unittest.TestCase):
    def test_run_wraps_when_available_and_not_otherwise(self) -> None:
        base = _outside_tmp()
        self.addCleanup(shutil.rmtree, base, True)
        policy = sandbox.build_policy(sandbox.Settings(), base, home=base / "h", make_caches=False)
        seen = []
        fake = lambda argv, **kwargs: seen.append(argv) or SimpleNamespace(pid=1)  # noqa: E731
        with mock.patch.object(sandbox, "availability", return_value=sandbox.Availability(True, "bwrap")):
            sandbox.run(["true"], cwd=base, env={}, policy=policy, popen=fake, platform_name="linux")
        with mock.patch.object(sandbox, "availability", return_value=sandbox.Availability(False, "bwrap", "x", "y")):
            sandbox.run(["true"], cwd=base, env={}, policy=policy, popen=fake, platform_name="linux")
        sandbox.run(["true"], cwd=base, env={}, policy=None, popen=fake)
        self.assertEqual(seen[0][0], linux.bwrap_path() or "bwrap")
        self.assertEqual(seen[1], ["true"])
        self.assertEqual(seen[2], ["true"])


class BlockedPathTests(unittest.TestCase):
    def test_parses_common_messages(self) -> None:
        text = "\n".join([
            "touch: cannot touch '/home/u/x': Read-only file system",
            "OSError: [Errno 30] Read-only file system: '/home/u/.cache/y'",
            "java.io.FileNotFoundException: /home/u/.gradle/z.lock (Read-only file system)",
            "mkdir: cannot create directory ‘/opt/sdk’: Read-only file system",
        ])
        self.assertEqual(sorted(sandbox.blocked_paths(text)), ["/home/u/.cache/y", "/home/u/.gradle/z.lock", "/home/u/x", "/opt/sdk"])

    def test_escape_target_never_offers_home_root_or_secrets(self) -> None:
        base = _outside_tmp()
        self.addCleanup(shutil.rmtree, base, True)
        (base / "home" / ".ssh").mkdir(parents=True)
        (base / "home" / "tools" / "sdk").mkdir(parents=True)
        (base / "ws").mkdir()
        policy = sandbox.build_policy(sandbox.Settings(), base / "ws", home=base / "home", make_caches=False)
        self.assertIsNone(sandbox.escape_target(str(base / "home" / "x"), policy))
        self.assertIsNone(sandbox.escape_target("/x", policy))
        self.assertIsNone(sandbox.escape_target(str(base / "home" / ".ssh" / "known_hosts"), policy))
        self.assertEqual(sandbox.escape_target(str(base / "home" / "tools" / "sdk" / "f"), policy),
                         (base / "home" / "tools" / "sdk").resolve())


class UseSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.session = agent_shell.ShellSession(self.ws)
        self.addCleanup(self.session.close)
        self.records: list[dict] = []
        audit._sink = self.records
        self.addCleanup(setattr, audit, "_sink", None)
        agent_shell._SANDBOX_NOTED = False

    def test_unavailable_keeps_todays_behavior_with_one_note(self) -> None:
        missing = sandbox.Availability(False, "bwrap", "bubblewrap (bwrap) is not installed", "install it")
        with mock.patch.object(sandbox, "availability", return_value=missing):
            first = agent_shell.use_sandbox(self.session, {})
            second = agent_shell.use_sandbox(self.session, {})
        self.assertIsNone(self.session.sandbox)
        self.assertIn("not sandboxed", first)
        self.assertIn("crit doctor sandbox", first)
        self.assertEqual(second, "", "said once per process")
        self.assertEqual(self.records[0]["event"], "unavailable")

    def test_an_incomplete_env_scan_is_said_and_audited(self) -> None:
        with mock.patch.object(sandbox, "availability", return_value=sandbox.Availability(True, "bwrap")), \
                mock.patch.object(sandbox, "ENV_SCAN_DIRS", 0):
            note = agent_shell.use_sandbox(self.session, {}, offline=True)
        self.assertFalse(self.session.sandbox.env_scan_complete)
        self.assertIn("every .env file", note)
        self.assertIn("env_scan_incomplete", [item.get("event") for item in self.records])

    def test_full_mode_turns_it_off(self) -> None:
        note = agent_shell.use_sandbox(self.session, {"sandbox": "full"})
        self.assertIsNone(self.session.sandbox)
        self.assertIn("off", note)

    def test_invalid_setting_falls_back_to_the_default(self) -> None:
        with mock.patch.object(sandbox, "availability", return_value=sandbox.Availability(True, "bwrap")):
            note = agent_shell.use_sandbox(self.session, {"sandbox": "sometimes"}, offline=True)
        self.assertIn("sandbox setting ignored", note)
        self.assertEqual(self.session.sandbox.mode, "workspace-write")

    def test_a_sandboxed_command_is_contained_for_auto_mode(self) -> None:
        ctx = agent_tools.ToolContext(workspace=self.ws, session=self.session)
        plain = agent_tools.permission_for("run_command", {"command": "make"}, ctx)
        self.assertFalse(plain.contained)
        with mock.patch.object(sandbox, "availability", return_value=sandbox.Availability(True, "bwrap")):
            agent_shell.use_sandbox(self.session, {}, offline=True)
        boxed = agent_tools.permission_for("run_command", {"command": "make"}, ctx)
        self.assertTrue(boxed.contained)
        network = agent_tools.permission_for("run_command", {"command": "curl https://x.example"}, ctx)
        self.assertFalse(network.contained)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"hello from the host"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@unittest.skipUnless(BWRAP, "bubblewrap is not usable here")
class EscapeTests(unittest.TestCase):
    """Real runs under bwrap. Each escape must fail closed; allowed paths must work."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = _outside_tmp()
        cls.home = cls.base / "home"
        cls.ws = cls.base / "ws"
        (cls.home / ".ssh").mkdir(parents=True)
        (cls.home / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n", encoding="utf-8")
        (cls.home / ".gradle" / "caches").mkdir(parents=True)
        (cls.home / ".m2" / "repository").mkdir(parents=True)
        (cls.home / ".npmrc").write_text("//registry.npmjs.org/:_authToken=FAKE-NPM", encoding="utf-8")
        (cls.ws / ".crit").mkdir(parents=True)
        (cls.ws / ".crit" / "settings.json").write_text("{}", encoding="utf-8")
        (cls.ws / ".env").write_text("API_TOKEN=abc", encoding="utf-8")
        (cls.ws / "services" / "api" / "config").mkdir(parents=True)
        (cls.ws / "services" / "api" / "config" / ".env").write_text("DEEP_TOKEN=abc", encoding="utf-8")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.proxy = egress_proxy.EgressProxy(ports=(cls.port,), restricted=lambda: False).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy.stop()
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self) -> None:
        taint.reset()  # taint is process-wide; an earlier test may have left it raised
        self.addCleanup(taint.reset)
        self.records: list[dict] = []
        audit._sink = self.records
        self.addCleanup(setattr, audit, "_sink", None)
        egress.configure("strict", "https://chat.example.com", ("allowed.example",))
        self.addCleanup(egress.configure, "strict")

    def run_in(self, argv: list[str], mode: str = "workspace-write", network: bool = True) -> tuple[int, str]:
        policy = sandbox.build_policy(
            sandbox.Settings(mode=mode, ports=(self.port,)), self.ws, home=self.home, make_caches=False,
            proxy_socket=self.proxy.socket_path, proxy_port=self.proxy.port, network=network,
        )
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home)}
        proc = sandbox.run(argv, cwd=self.ws, env=env, policy=policy, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=60)
        return proc.returncode, out.decode("utf-8", "replace")

    def py(self, code: str, **kwargs: object) -> tuple[int, str]:
        return self.run_in([sys.executable, "-I", "-c", code], **kwargs)

    def test_writes_in_the_workspace_and_caches_work(self) -> None:
        code, out = self.run_in(["/bin/sh", "-c", f"echo ok > built.txt && echo c > '{self.home}/.gradle/caches/cache.bin' "
                                 f"&& mkdir -p '{self.home}/.m2/repository/org/x' && cat built.txt"])
        self.assertEqual(code, 0, out)
        self.assertTrue((self.ws / "built.txt").exists())
        self.assertTrue((self.home / ".gradle" / "caches" / "cache.bin").exists())
        self.assertTrue((self.home / ".m2" / "repository" / "org" / "x").is_dir())

    def test_auto_run_files_in_caches_cannot_be_planted(self) -> None:
        # SB-2: Gradle init scripts, gradle.properties and Maven's settings.xml run or steer a later build.
        for target in (".gradle/init.gradle", ".gradle/init.d/x.gradle", ".gradle/gradle.properties", ".m2/settings.xml"):
            code, out = self.run_in(["/bin/sh", "-c", f"mkdir -p \"$(dirname '{self.home}/{target}')\"; "
                                     f"echo planted > '{self.home}/{target}'"])
            self.assertNotEqual(code, 0, target)
            self.assertFalse((self.home / target).exists(), target)

    def test_credential_files_and_deep_env_files_read_empty(self) -> None:
        # SB-1 and SB-3.
        code, out = self.run_in(["/bin/sh", "-c", "cat ~/.npmrc services/api/config/.env 2>&1; true"])
        self.assertNotIn("FAKE-NPM", out)
        self.assertNotIn("DEEP_TOKEN", out)

    def test_write_outside_the_workspace_fails(self) -> None:
        code, out = self.run_in(["/bin/sh", "-c", f"echo x > '{self.home}/x'"])
        self.assertNotEqual(code, 0)
        self.assertIn("Read-only file system", out)
        self.assertFalse((self.home / "x").exists())

    def test_read_only_mode_cannot_write_the_workspace(self) -> None:
        code, out = self.run_in(["/bin/sh", "-c", "echo x > ro.txt"], mode="read-only")
        self.assertNotEqual(code, 0)
        self.assertFalse((self.ws / "ro.txt").exists())

    def test_crit_settings_stay_read_only(self) -> None:
        code, _out = self.run_in(["/bin/sh", "-c", "echo '{\"permissions\": \"auto\"}' > .crit/settings.json"])
        self.assertNotEqual(code, 0)
        self.assertEqual((self.ws / ".crit" / "settings.json").read_text(encoding="utf-8"), "{}")

    def test_a_private_key_cannot_be_read(self) -> None:
        code, out = self.run_in(["/bin/sh", "-c", f"cat '{self.home}/.ssh/id_ed25519'"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("PRIVATE KEY", out)

    def test_env_files_are_masked(self) -> None:
        code, out = self.run_in(["/bin/sh", "-c", "cat .env"])
        self.assertNotIn("API_TOKEN", out)

    def test_a_direct_connection_fails(self) -> None:
        code, out = self.py("import socket; socket.create_connection(('1.1.1.1', 443), 3)")
        self.assertNotEqual(code, 0, out)

    def test_dns_lookup_fails(self) -> None:
        code, out = self.py("import socket; print(socket.getaddrinfo('example.com', 443))")
        self.assertNotEqual(code, 0, out)
        self.assertIn("gaierror", out)

    def test_a_host_loopback_port_that_is_not_forwarded_is_unreachable(self) -> None:
        import socket

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        code, out = self.py(f"import socket; socket.create_connection(('127.0.0.1', {port}), 3)")
        self.assertNotEqual(code, 0, out)
        code, out = self.py(
            "import urllib.request\n"
            f"try:\n urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=5)\n"
            "except Exception as exc: print('ERR', exc)\n")
        self.assertIn("ERR", out)

    def test_proxy_denies_a_host_off_the_allow_list(self) -> None:
        code, out = self.py(
            "import urllib.request\n"
            "try:\n urllib.request.urlopen('http://evil.example.org/x', timeout=10)\n"
            "except Exception as exc: print('ERR', exc)\n")
        self.assertIn("403", out)
        denied = [item for item in self.records if item["kind"] == "egress" and not item["allowed"]]
        self.assertEqual(denied[-1]["host"], "evil.example.org")

    def test_a_forwarded_host_port_works_through_the_bridge(self) -> None:
        code, out = self.py(
            f"import socket; s = socket.create_connection(('127.0.0.1', {self.port}), 5);"
            "s.sendall(b'GET / HTTP/1.0\\r\\n\\r\\n'); data = b''\n"
            "while True:\n chunk = s.recv(4096)\n if not chunk: break\n data += chunk\n"
            "print(data.decode())")
        self.assertEqual(code, 0, out)
        self.assertIn("hello from the host", out)

    def test_shell_session_runs_sandboxed_and_audits(self) -> None:
        session = agent_shell.ShellSession(self.ws)
        self.addCleanup(session.close)
        with mock.patch.object(sandbox, "build_policy", wraps=sandbox.build_policy) as built:
            note = agent_shell.use_sandbox(session, {}, offline=True, home=self.home)
        self.assertTrue(built.called)
        self.assertIn("sandboxed (workspace-write", note)
        result = session.run(f"echo hi > in.txt; echo x > '{self.home}/out.txt'; cat in.txt", timeout=60)
        self.assertIn("hi", result.stdout)
        self.assertFalse((self.home / "out.txt").exists())
        commands = [item for item in self.records if item["kind"] == "command"]
        self.assertEqual(commands[-1]["sandbox"], "workspace-write")

    def test_an_escape_asks_once_and_runs_again(self) -> None:
        tools = self.home / "tools"
        tools.mkdir(exist_ok=True)
        session = agent_shell.ShellSession(self.ws)
        self.addCleanup(session.close)
        agent_shell.use_sandbox(session, {}, offline=True, home=self.home)
        asked: list[str] = []
        ctx = agent_tools.ToolContext(workspace=self.ws, session=session, confirm=lambda q: asked.append(q) or "yes")
        result = agent_tools.execute("run_command", {"command": f"touch '{tools}/made'"}, ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(asked), 1)
        self.assertIn(str(tools.resolve()), asked[0])
        self.assertTrue((tools / "made").exists())
        self.assertIn("the user allowed", result["output"])
        # remembered for the session: no second question
        again = agent_tools.execute("run_command", {"command": f"touch '{tools}/again'"}, ctx)
        self.assertTrue(again["ok"], again)
        self.assertEqual(len(asked), 1)

    def test_an_escape_is_not_offered_while_tainted_in_auto(self) -> None:
        tools = self.home / "tools2"
        tools.mkdir(exist_ok=True)
        session = agent_shell.ShellSession(self.ws)
        self.addCleanup(session.close)
        agent_shell.use_sandbox(session, {}, offline=True, home=self.home)
        taint.reset()
        self.addCleanup(taint.reset)
        taint.mark("web_fetch", "https://evil.example/")
        taint.state().mode = "auto"
        asked: list[str] = []
        ctx = agent_tools.ToolContext(workspace=self.ws, session=session, confirm=lambda q: asked.append(q) or "yes")
        result = agent_tools.execute("run_command", {"command": f"touch '{tools}/made'"}, ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(asked, [])
        self.assertIn("tainted", result["output"])


@unittest.skipUnless(BWRAP, "bubblewrap is not usable here")
class AutoModeTests(unittest.TestCase):
    """In auto mode a sandboxed command runs without a prompt; unsandboxed, the same command asks."""

    def run_loop(self, sandboxed: bool, available: bool = True) -> tuple[list[str], Path]:
        import io

        taint.reset()
        self.addCleanup(taint.reset)

        from rich.console import Console

        from critique_bot import agent, agent_ui

        root = _outside_tmp()
        self.addCleanup(shutil.rmtree, root, True)
        session = agent_shell.ShellSession(root)
        self.addCleanup(session.close)
        if sandboxed:
            found = sandbox.availability() if available else sandbox.Availability(False, "bwrap", "not installed")
            with mock.patch.object(sandbox, "availability", return_value=found):
                agent_shell.use_sandbox(session, {}, offline=True, home=root / "home")
        agent.use_ui(agent_ui)
        self.addCleanup(agent.use_ui, None)
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100, theme=agent_ui.rich_theme("dark")))
        self.addCleanup(agent_ui.reset)
        call = "<tool_call>\n" + json.dumps({"tool": "run_command", "arguments": {"command": "touch made.txt"}}) + "\n</tool_call>"
        replies = [call, "COMPLETED"]
        scripted = SimpleNamespace(last_detail=None, send=lambda _prompt: replies.pop(0))
        asked: list[str] = []
        with mock.patch("critique_bot.engine.ui._approve_prompt", lambda perm, **kw: asked.append(perm.summary) or ("no", "")):
            agent.run_agent_loop(scripted, workspace=root, index_path=None, cache_dir=None, first_task="make it",
                                 max_rounds=5, max_result_chars=8000, read_message=lambda: None, emit=lambda _t: None,
                                 ask_user=lambda _q: None, approve_mode="auto", session_shell=session)
        return asked, root

    def test_sandboxed_command_runs_unasked(self) -> None:
        asked, root = self.run_loop(sandboxed=True)
        self.assertEqual(asked, [])
        self.assertTrue((root / "made.txt").exists())

    def test_unsandboxed_command_still_asks(self) -> None:
        asked, root = self.run_loop(sandboxed=False)
        self.assertEqual(len(asked), 1)
        self.assertFalse((root / "made.txt").exists())

    def test_a_missing_backend_keeps_auto_mode_asking(self) -> None:
        # The sandbox fails open (commands run unwrapped), so auto mode must not treat them as contained.
        asked, root = self.run_loop(sandboxed=True, available=False)
        self.assertEqual(len(asked), 1)
        self.assertFalse((root / "made.txt").exists())


@unittest.skipUnless(BWRAP, "bubblewrap is not usable here")
class DoctorTests(unittest.TestCase):
    def test_live_checks_pass(self) -> None:
        from critique_bot.sandbox_doctor import live_checks

        base = _outside_tmp()
        self.addCleanup(shutil.rmtree, base, True)
        (base / "home" / ".ssh").mkdir(parents=True)
        (base / "home" / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(base / "home")}):
            results = live_checks(lambda ws: sandbox.build_policy(sandbox.Settings(), ws, home=base / "home", make_caches=False))
        self.assertTrue(all(passed for _name, passed, _detail in results), results)


class DoctorCliTests(unittest.TestCase):
    def test_reports_the_fix_when_unavailable(self) -> None:
        import contextlib
        import io

        from critique_bot.sandbox_doctor import doctor_sandbox

        missing = sandbox.Availability(False, "bwrap", "bubblewrap (bwrap) is not installed", linux.INSTALL_HINT)
        out = io.StringIO()
        with mock.patch.object(sandbox, "availability", return_value=missing), contextlib.redirect_stdout(out):
            code = doctor_sandbox(["--workspace", tempfile.gettempdir()])
        self.assertEqual(code, 1)
        self.assertIn("apt install bubblewrap", out.getvalue())
        self.assertIn("NOT AVAILABLE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
