from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH_SCRIPT = ROOT / "scripts" / "setup-crit.sh"
PS_SCRIPT = ROOT / "scripts" / "setup-crit.ps1"
WRAPPER_MARKER = "# critique-bot: written by scripts/setup-crit.sh"
RC_MARKER = "# critique-bot: added by scripts/setup-crit.sh"


def _installed_venv() -> Path | None:
    """A venv that already has the crit console scripts (no network needed)."""
    for candidate in (Path(sys.prefix), ROOT / ".venv"):
        if (candidate / "bin" / "crit").exists() and (candidate / "bin" / "python").exists():
            return candidate
    return None


VENV = _installed_venv()
BASH = shutil.which("bash")


@unittest.skipIf(os.name == "nt" or BASH is None, "setup-crit.sh needs bash")
class SetupCritShTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Spaces in every path check the quoting in wrappers and the rc line.
        self.tmp = Path(tmp.name) / "with space"
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True)
        self.bin_dir = self.tmp / "bin dir"
        self.config = self.tmp / "cfg dir" / "config.json"

    def _run(self, *extra: str, shell: str = "/bin/bash", check: bool = True) -> subprocess.CompletedProcess:
        if VENV is None:
            self.skipTest("no installed venv with the crit console script")
        env = dict(os.environ)
        env.update(HOME=str(self.home), SHELL=shell, CRIT_SETUP_SKIP_INSTALL="1")
        cmd = [
            BASH, str(SH_SCRIPT),
            "--venv", str(VENV),
            "--config", str(self.config),
            "--bin-dir", str(self.bin_dir),
            "--skip-browser-check",
            *extra,
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def _snapshot(self) -> dict[str, tuple[str, int]]:
        files = {}
        for path in sorted(self.tmp.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(self.tmp))] = (path.read_text(encoding="utf-8"), path.stat().st_mode)
        return files

    def test_bash_syntax(self) -> None:
        proc = subprocess.run([BASH, "-n", str(SH_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_script_is_executable(self) -> None:
        self.assertTrue(os.access(SH_SCRIPT, os.X_OK))

    @unittest.skipIf(shutil.which("shellcheck") is None, "shellcheck not installed")
    def test_shellcheck_clean(self) -> None:
        proc = subprocess.run(["shellcheck", str(SH_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_help(self) -> None:
        proc = subprocess.run([BASH, str(SH_SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        for flag in ("--venv", "--config", "--bin-dir", "--proxy", "--with-index", "--no-path",
                     "--skip-browser-check", "--install-deps"):
            self.assertIn(flag, proc.stdout)

    def test_unknown_option_fails(self) -> None:
        proc = subprocess.run([BASH, str(SH_SCRIPT), "--bogus"], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown option", proc.stderr)

    def test_proxy_requires_a_url(self) -> None:
        missing = subprocess.run([BASH, str(SH_SCRIPT), "--proxy"], capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("needs a value", missing.stderr)
        bad = subprocess.run(
            [BASH, str(SH_SCRIPT), "--proxy", "10.1.2.3:8080"], capture_output=True, text=True,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("must be a URL", bad.stderr)

    def test_proxy_is_passed_to_every_pip_install(self) -> None:
        """A stand-in venv records pip's argv so both installs get --proxy."""
        proxy = "http://alice:s3cret@10.1.2.3:8080"
        venv = self.tmp / "venv"
        bindir = venv / "bin"
        bindir.mkdir(parents=True)
        log = self.tmp / "pip-args.log"
        python = bindir / "python"
        python.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{log}'\n"
            'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
            '  if [ "${3:-}" = "--version" ]; then echo "pip 24.0"; exit 0; fi\n'
            "  exit 0\n"
            "fi\n"
            'exec /usr/bin/python3 "$@"\n',
            encoding="utf-8",
        )
        python.chmod(0o755)
        stub = "#!/bin/sh\necho 'usage: crit'\nexit 0\n"
        for name in ("crit", "bot-agent", "critique-bot"):
            exe = bindir / name
            exe.write_text(stub, encoding="utf-8")
            exe.chmod(0o755)

        env = dict(os.environ)
        env.update(HOME=str(self.home), SHELL="/bin/bash")
        env.pop("CRIT_SETUP_SKIP_INSTALL", None)
        proc = subprocess.run(
            [BASH, str(SH_SCRIPT), "--venv", str(venv), "--config", str(self.config),
             "--bin-dir", str(self.bin_dir), "--skip-browser-check", "--no-path",
             "--proxy", proxy],
            env=env, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("proxy:   http://***@10.1.2.3:8080", proc.stdout)
        self.assertNotIn("s3cret", proc.stdout)
        lines = log.read_text(encoding="utf-8").splitlines()
        installs = [line for line in lines if " install " in line]
        self.assertEqual(len(installs), 2, lines)
        for line in installs:
            self.assertTrue(line.endswith(f"--proxy {proxy}"), line)
        version_lines = [line for line in lines if line.endswith("pip --version") or "pip --version" in line]
        self.assertTrue(version_lines, lines)
        for line in version_lines:
            self.assertNotIn("--proxy", line)

    def test_install_writes_wrappers_and_config(self) -> None:
        proc = self._run("--no-path")
        self.assertIn("==> Writing commands", proc.stdout)
        self.assertIn("critique-bot setup --config", proc.stdout)
        self.assertIn("PONG", proc.stdout)

        expected = {
            "crit": f"exec '{VENV}/bin/crit' --config '{self.config}' \"$@\"",
            "bot-agent": f"exec '{VENV}/bin/bot-agent' --config '{self.config}' \"$@\"",
            "critique-bot": f"exec '{VENV}/bin/critique-bot' \"$@\"",
        }
        for name, line in expected.items():
            wrapper = self.bin_dir / name
            self.assertEqual(
                wrapper.read_text(encoding="utf-8"),
                f"#!/bin/sh\n{WRAPPER_MARKER}\n{line}\n",
            )
            self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)

        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["user_data_dir"], str(self.config.parent / ".edge-profile"))
        self.assertEqual(list(data), list(example))
        for key, value in example.items():
            if key != "user_data_dir":
                self.assertEqual(data[key], value)
        self.assertFalse((self.home / ".bashrc").exists())

        # crit --help works through the wrapper from another folder.
        help_proc = subprocess.run(
            [str(self.bin_dir / "crit"), "--help"], cwd=self.tmp,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
        self.assertIn("usage", help_proc.stdout.lower())

    def test_rerun_is_idempotent(self) -> None:
        self._run()
        before = self._snapshot()
        proc = self._run()
        self.assertEqual(self._snapshot(), before)
        self.assertIn("is up to date", proc.stdout)
        self.assertIn("already has the PATH line", proc.stdout)
        self.assertEqual(list(self.bin_dir.glob("*.bak*")), [])

    def test_path_line_added_once_to_bashrc(self) -> None:
        (self.home / ".bashrc").write_text("# existing\n", encoding="utf-8")
        self._run()
        self._run()
        text = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# existing\n"))
        self.assertEqual(text.count(RC_MARKER), 1)
        self.assertEqual(text.count(f'export PATH="{self.bin_dir}:$PATH"'), 1)
        # The line works when sourced.
        out = subprocess.run(
            [BASH, "-c", f'PATH=/usr/bin:/bin; . "{self.home}/.bashrc"; command -v crit'],
            capture_output=True, text=True,
        )
        self.assertEqual(out.stdout.strip(), str(self.bin_dir / "crit"))

    def test_zsh_uses_zshrc(self) -> None:
        self._run(shell="/bin/zsh")
        self.assertIn(RC_MARKER, (self.home / ".zshrc").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".bashrc").exists())

    def test_bin_dir_already_on_path_leaves_rc_alone(self) -> None:
        if VENV is None:
            self.skipTest("no installed venv")
        self.bin_dir.mkdir(parents=True)
        env_path = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        env = dict(os.environ, HOME=str(self.home), SHELL="/bin/bash",
                   CRIT_SETUP_SKIP_INSTALL="1", PATH=env_path)
        proc = subprocess.run(
            [BASH, str(SH_SCRIPT), "--venv", str(VENV), "--config", str(self.config),
             "--bin-dir", str(self.bin_dir), "--skip-browser-check"],
            env=env, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("already on PATH", proc.stdout)
        self.assertFalse((self.home / ".bashrc").exists())

    def test_foreign_wrapper_is_backed_up_once(self) -> None:
        self.bin_dir.mkdir(parents=True)
        foreign = self.bin_dir / "crit"
        foreign.write_text("#!/bin/sh\necho someone else\n", encoding="utf-8")
        self._run("--no-path")
        self.assertEqual((self.bin_dir / "crit.bak").read_text(encoding="utf-8"),
                         "#!/bin/sh\necho someone else\n")
        self.assertIn(WRAPPER_MARKER, foreign.read_text(encoding="utf-8"))
        self._run("--no-path")
        self.assertEqual(sorted(p.name for p in self.bin_dir.glob("*.bak*")), ["crit.bak"])

    def test_own_old_wrapper_is_replaced_without_backup(self) -> None:
        self.bin_dir.mkdir(parents=True)
        (self.bin_dir / "crit").write_text(f"#!/bin/sh\n{WRAPPER_MARKER}\nexec /old/crit \"$@\"\n",
                                           encoding="utf-8")
        self._run("--no-path")
        self.assertNotIn("/old/crit", (self.bin_dir / "crit").read_text(encoding="utf-8"))
        self.assertEqual(list(self.bin_dir.glob("*.bak*")), [])

    def test_existing_config_user_data_dir_rules(self) -> None:
        self.config.parent.mkdir(parents=True)
        for value, expected in (
            ("profile dir", str(self.config.parent / "profile dir")),
            ("system", "system"),
            ("/abs/profile", "/abs/profile"),
            ("", str(self.config.parent / ".edge-profile")),
        ):
            with self.subTest(value=value):
                self.config.write_text(
                    json.dumps({"url": "https://x/", "user_data_dir": value, "z": 1}, indent=2) + "\n",
                    encoding="utf-8",
                )
                self._run("--no-path")
                data = json.loads(self.config.read_text(encoding="utf-8"))
                self.assertEqual(data["user_data_dir"], expected)
                self.assertEqual(list(data), ["url", "user_data_dir", "z"])
                self.assertEqual(data["url"], "https://x/")


PWSH = os.environ.get("CRIT_TEST_PWSH")


@unittest.skipUnless(PWSH, "set CRIT_TEST_PWSH to a pwsh executable to check setup-crit.ps1")
class SetupCritPs1Tests(unittest.TestCase):
    def _pwsh(self, command: str, **env: str) -> subprocess.CompletedProcess:
        full_env = dict(os.environ, **env)
        proc = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=120, env=full_env,
        )
        return proc

    def test_parses_without_errors(self) -> None:
        script = str(PS_SCRIPT).replace("'", "''")
        proc = self._pwsh(
            "$t=$null; $e=$null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$t, [ref]$e); "
            "$e | ForEach-Object { $_.ToString() }; 'COUNT=' + @($e).Count"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("COUNT=0", proc.stdout, proc.stdout)

    def test_no_pwsh_only_operators(self) -> None:
        # Windows PowerShell 5.1 has no ?? / ?. / && / || pipeline chains / ternary.
        script = str(PS_SCRIPT).replace("'", "''")
        proc = self._pwsh(
            "$t=$null; $e=$null; "
            f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$t, [ref]$e); "
            "$bad = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.TernaryExpressionAst] "
            "-or $n -is [System.Management.Automation.Language.PipelineChainAst] "
            "-or ($n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator -eq 'QuestionQuestion') }, $true); "
            "'BAD=' + @($bad).Count"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BAD=0", proc.stdout)

    def test_get_help_lists_parameters(self) -> None:
        script = str(PS_SCRIPT).replace("'", "''")
        proc = self._pwsh(f"$h = Get-Help '{script}' -Full; $h.Synopsis; $h.parameters.parameter.name")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("critique-bot", proc.stdout)
        for name in ("Venv", "Config", "BinDir", "Proxy", "WithIndex", "NoPath", "SkipBrowserCheck"):
            self.assertIn(name, proc.stdout.split())

    def test_helpers(self) -> None:
        script = str(PS_SCRIPT).replace("'", "''")
        proc = self._pwsh(
            f". '{script}'; "
            "'MERGED=' + (Get-MergedPath '%USERPROFILE%\\bin;C:\\x\\bin\\;;D:\\y;c:\\X\\BIN;D:\\y' 'C:\\x\\bin'); "
            "'EMPTY=' + (Get-MergedPath '' 'C:\\x\\bin'); "
            "'AGAIN=' + (Get-MergedPath 'C:\\x\\bin;%USERPROFILE%\\bin;D:\\y' 'C:\\x\\bin'); "
            "'WRAP=' + ((Get-WrapperText 'C:\\a b\\.venv\\Scripts\\crit.exe' 'C:\\a b\\config.json') -replace \"`r`n\", '|'); "
            "'PLAIN=' + ((Get-WrapperText 'C:\\a%b\\critique-bot.exe' '') -replace \"`r`n\", '|'); "
            "'REDACT=' + (Get-RedactedProxy 'http://username:secret@10.1.2.3:8080'); "
            "'NOPASS=' + (Get-RedactedProxy 'http://10.1.2.3:8080'); "
            "'PIP=' + ((Get-PipInstallArgs 'http://username:secret@10.1.2.3:8080' @('install','--quiet','-e','C:\\repo')) -join ' '); "
            "'NOPROXY=' + ((Get-PipInstallArgs '' @('install','--quiet','-e','C:\\repo')) -join ' ')",
            CRIT_SETUP_NO_MAIN="1",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        self.assertEqual(lines["MERGED"], "C:\\x\\bin;%USERPROFILE%\\bin;D:\\y")
        self.assertEqual(lines["EMPTY"], "C:\\x\\bin")
        self.assertEqual(lines["AGAIN"], "C:\\x\\bin;%USERPROFILE%\\bin;D:\\y")
        self.assertEqual(
            lines["WRAP"],
            '@echo off|rem critique-bot: written by scripts\\setup-crit.ps1|'
            '"C:\\a b\\.venv\\Scripts\\crit.exe" --config "C:\\a b\\config.json" %*|',
        )
        self.assertEqual(
            lines["PLAIN"],
            '@echo off|rem critique-bot: written by scripts\\setup-crit.ps1|"C:\\a%%b\\critique-bot.exe" %*|',
        )
        self.assertEqual(lines["REDACT"], "http://***@10.1.2.3:8080")
        self.assertEqual(lines["NOPASS"], "http://10.1.2.3:8080")
        self.assertEqual(
            lines["PIP"],
            "install --quiet -e C:\\repo --proxy http://username:secret@10.1.2.3:8080",
        )
        self.assertEqual(lines["NOPROXY"], "install --quiet -e C:\\repo")

    def test_refuses_to_run_off_windows(self) -> None:
        if os.name == "nt":
            self.skipTest("runs the real setup on Windows")
        proc = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(PS_SCRIPT), "-SkipBrowserCheck"],
            capture_output=True, text=True, timeout=120,
            env={k: v for k, v in os.environ.items() if k != "CRIT_SETUP_NO_MAIN"},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("setup-crit.sh", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
