"""Shell execution for ``run_command``.

On Windows the command runs in PowerShell 7 (``pwsh.exe``) when it is on
PATH, else Windows PowerShell 5.1. Each call is a new process, so ``cd`` does
not carry over; the ``cwd`` argument does. A timeout stops the whole process
tree, not only the shell, so a hung gradle or python child does not linger.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
_PROMPT_RE = re.compile(
    r"(\[y/n\]|\(y/n\)|\[Y/n\]|\[y/N\]|press any key|continue\?|password:|passphrase|"
    r"are you sure|overwrite\?|enter to continue)\s*$",
    re.IGNORECASE,
)
HEAD_LINES = 40
TAIL_LINES = 120

QUIET_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "TERM": "dumb",
    "CI": "1",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "npm_config_yes": "true",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "POWERSHELL_TELEMETRY_OPTOUT": "1",
}


@dataclass(frozen=True)
class Shell:
    kind: str
    exe: str
    label: str

    @property
    def is_powershell(self) -> bool:
        return self.kind in {"pwsh", "powershell"}


def shell_preamble(shell: Shell) -> str:
    """The first lines of the system prompt: which shell ``run_command`` actually is."""
    if shell.kind == "powershell":
        return (
            "SHELL: Windows PowerShell 5.1 (powershell.exe). "
            "run_command uses this shell and no other. "
            "Chain with ; and test $? or $LASTEXITCODE. && and || do not work. "
            "Use PowerShell cmdlets, not grep, sed, awk, cat, export, or ls -la."
        )
    if shell.kind == "pwsh":
        return (
            "SHELL: PowerShell 7 (pwsh.exe). "
            "run_command uses this shell and no other. "
            "&& and || work. Use PowerShell cmdlets, not grep, sed, awk, or cat."
        )
    return "SHELL: bash -lc. run_command uses this shell and no other."


def detect_shell(platform_name: str | None = None, which: Callable[[str], str | None] | None = None) -> Shell:
    plat = platform_name if platform_name is not None else sys.platform
    find = which or shutil.which
    if plat == "win32":
        pwsh = find("pwsh.exe") or find("pwsh")
        if pwsh:
            return Shell("pwsh", "pwsh.exe", "PowerShell 7 (pwsh.exe)")
        return Shell("powershell", "powershell.exe", "Windows PowerShell 5.1 (powershell.exe)")
    return Shell("bash", "bash", "bash -lc")


def command_argv(command: str, *, platform_name: str | None = None, shell: Shell | None = None) -> list[str]:
    """Build the argv for one command. The command text is one argument."""
    chosen = shell or detect_shell(platform_name)
    if chosen.is_powershell:
        return _powershell_argv(command, chosen.exe)
    return ["bash", "-lc", command]


def _powershell_argv(command: str, exe: str = "powershell.exe") -> list[str]:
    """Run one PowerShell command and return its real exit code and UTF-8 text.

    Windows PowerShell writes UTF-16 when stdout is a pipe, and a native
    program's exit code stays in ``$LASTEXITCODE`` instead of the process
    code. The wrapper fixes both so the tool result is the text the command
    printed. A terminating error exits 1 with its message on stderr.
    """
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "$OutputEncoding = [Console]::OutputEncoding\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$ConfirmPreference = 'None'\n"
        "try {\n"
        f"{command.rstrip()}\n"
        "} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }\n"
        "if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
        "if (-not $?) { exit 1 }\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


_BASHISMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![&|])&&(?![&|])"), "&& is not PowerShell 5.1 syntax. Use ; or: cmd1; if ($?) { cmd2 }"),
    (re.compile(r"(?<![|])\|\|(?![|])"), "|| is not PowerShell 5.1 syntax. Use: cmd1; if (-not $?) { cmd2 }"),
    (re.compile(r"^\s*export\s+\w+="), "export is bash. Use: $env:NAME = 'value'"),
    (re.compile(r"(^|[;&|]\s*)\w+=\S+\s+\w"), "VAR=value cmd is bash. Use: $env:VAR = 'value'; cmd"),
    (re.compile(r"<<\s*['\"]?\w+"), "Heredoc (<<EOF) is bash. Write the file with write_files, or use a here-string @' ... '@"),
    (re.compile(r"(^|[;|]\s*)(grep|sed|awk|xargs|head|tail|touch|which)\b"),
     "That is a Unix tool. Use search_code / read_files, or Select-String, Get-Content -TotalCount / -Tail, New-Item, Get-Command"),
    (re.compile(r"\bls\s+-[a-zA-Z]+"), "ls flags are bash. Use: Get-ChildItem -Force (or list_files)"),
    (re.compile(r"\brm\s+-[rRf]+"), "rm -rf is bash. Use: Remove-Item -Recurse -Force <path>"),
    (re.compile(r"\bmkdir\s+-p\b"), "mkdir -p is bash. Use: New-Item -ItemType Directory -Force <path>"),
    (re.compile(r"\bcat\s+[^|;]*>"), "cat > file is bash. Use write_files"),
    (re.compile(r"(?<![\w$])/dev/null\b"), "/dev/null is bash. Use $null or | Out-Null"),
    (re.compile(r"\bpython3\b"), "python3 is usually missing on Windows. Use python, py, or the venv python shown in ENVIRONMENT"),
)


def lint_command(command: str, shell: Shell) -> str | None:
    """A precise message when the command uses syntax this shell cannot run."""
    if not shell.is_powershell:
        return None
    for pattern, message in _BASHISMS:
        if shell.kind == "pwsh" and ("&&" in message or "||" in message):
            continue
        if pattern.search(command):
            return f"not run: {message}. Shell is {shell.label}."
    return None


def environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(QUIET_ENV)
    if extra:
        env.update(extra)
    return env


def adjust_command(command: str) -> str:
    """Small rewrites that keep build tools from waiting on a terminal."""
    if re.search(r"(^|[\s;&|\\/.])gradlew(\.bat)?\b", command) and "--console" not in command:
        command = re.sub(r"(gradlew(?:\.bat)?)", r"\1 --console=plain", command, count=1)
    return command


def tool_hints(workspace: Path, platform_name: str | None = None) -> list[str]:
    """Interpreters and wrappers the model should use, for the ENVIRONMENT block."""
    plat = platform_name if platform_name is not None else sys.platform
    root = Path(workspace)
    hints: list[str] = []
    for folder in (".venv", "venv", "env"):
        if plat == "win32":
            candidate = root / folder / "Scripts" / "python.exe"
            shown = f"{folder}\\Scripts\\python.exe"
        else:
            candidate = root / folder / "bin" / "python"
            shown = f"{folder}/bin/python"
        if candidate.is_file():
            hints.append(f"python: {shown} (project virtualenv; use it instead of the system python)")
            break
    if (root / "gradlew.bat").is_file() and plat == "win32":
        hints.append("gradle: .\\gradlew.bat")
    elif (root / "gradlew").is_file():
        hints.append("gradle: ./gradlew")
    if (root / "package.json").is_file():
        hints.append("node: npm (package.json present)")
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        hints.append("python project: pyproject.toml present")
    return hints


def run(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    shell: Shell | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[int | None, str, str, float]:
    """Run one command. Returns exit code (None on timeout), stdout, stderr, seconds."""
    chosen = shell or detect_shell()
    argv = command_argv(adjust_command(command), shell=chosen)
    started = time.monotonic()
    if runner is not None:
        try:
            proc = runner(
                argv,
                cwd=str(cwd),
                capture_output=True,
                check=False,
                timeout=timeout,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            return None, decode(exc.stdout), decode(exc.stderr), time.monotonic() - started
        code = int(getattr(proc, "returncode", 1) or 0)
        return code, decode(getattr(proc, "stdout", b"")), decode(getattr(proc, "stderr", b"")), time.monotonic() - started
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment(),
        **kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return None, decode(out), decode(err), time.monotonic() - started
    return int(proc.returncode or 0), decode(out), decode(err), time.monotonic() - started


def kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def decode(data: object) -> str:
    if isinstance(data, str):
        return data
    if not isinstance(data, (bytes, bytearray)):
        return ""
    raw = bytes(data)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    if len(raw) >= 4 and raw[1] == 0 and raw[3] == 0:
        return raw.decode("utf-16-le", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        import locale

        try:
            return raw.decode(locale.getpreferredencoding(False) or "cp1252", "replace")
        except LookupError:
            return raw.decode("utf-8", "replace")


def clean_clixml(text: str) -> str:
    """Turn a PowerShell CLIXML error record into the message it carried."""
    if "#<" not in text or "CLIXML" not in text:
        return text
    messages = re.findall(r"<S\b[^>]*>(.*?)</S>", text, re.DOTALL)
    if not messages:
        return re.sub(r"#<\s*CLIXML[\s\S]*", "", text).strip()
    lines: list[str] = []
    for message in messages:
        decoded = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda match: chr(int(match.group(1), 16)), message)
        decoded = decoded.replace("\r", "").strip()
        if decoded:
            lines.append(decoded)
    return "\n".join(lines)


def tidy(text: str) -> str:
    """Strip ANSI, carriage-return progress redraws, and runs of repeated lines."""
    text = clean_clixml(_ANSI_RE.sub("", text))
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rsplit("\r", 1)[-1]
        out.append(line.rstrip())
    collapsed: list[str] = []
    repeat = 0
    for line in out:
        if collapsed and line == collapsed[-1] and line:
            repeat += 1
            continue
        if repeat:
            collapsed.append(f"... previous line repeated {repeat} more times")
            repeat = 0
        collapsed.append(line)
    if repeat:
        collapsed.append(f"... previous line repeated {repeat} more times")
    return "\n".join(collapsed).strip("\n")


def head_tail(text: str, *, head: int = HEAD_LINES, tail: int = TAIL_LINES, max_chars: int = 16_000) -> str:
    """Keep the start and the end of long output. Test summaries live at the end."""
    lines = text.split("\n")
    if len(lines) > head + tail + 1:
        dropped = len(lines) - head - tail
        lines = lines[:head] + [f"... {dropped} lines omitted ..."] + lines[-tail:]
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        keep_head = max_chars // 4
        keep_tail = max_chars - keep_head - 40
        joined = joined[:keep_head] + "\n... output trimmed ...\n" + joined[-keep_tail:]
    return joined


def waiting_for_input(text: str) -> bool:
    tail = text.strip().split("\n")[-1] if text.strip() else ""
    return bool(_PROMPT_RE.search(tail))
