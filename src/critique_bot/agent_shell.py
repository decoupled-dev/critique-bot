"""Shell execution for ``run_command``.

Shells
    Windows: PowerShell 7 (``pwsh.exe``) when installed, else Windows
    PowerShell 5.1; Git Bash and ``cmd.exe`` are available per call. WSL's
    ``System32\\bash.exe`` is never used. Linux/macOS: ``bash``, else ``sh``.
    Detection runs once per process and returns full executable paths.

How a command runs
    The command is written to a temporary script in a private temp folder
    (``.ps1`` UTF-8 with BOM, ``.cmd`` with ``chcp 65001``, or ``.sh``) and the
    shell runs that file, so there is no command-line length limit and no
    quoting layer. POSIX shells run without a login profile; the login
    environment is captured once per process instead. Children get a scrubbed
    environment (tokens, passwords and API keys removed).

Working folder
    :class:`ShellSession` keeps a current folder like a terminal: after each
    command the shell writes its final folder to a marker file and the next
    command starts there.

Output and limits
    stdout and stderr are read by threads into bounded head+tail buffers, so a
    noisy build cannot use unbounded memory. On timeout or cancel the whole
    process tree is killed and the output collected so far is returned. When
    the shell exits but a grandchild keeps the pipes open, the run returns
    about two seconds later.
"""

from __future__ import annotations

import atexit
import base64
import collections
import functools
import ntpath
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
_PROMPT_RE = re.compile(
    r"(\[y/n\]|\(y/n\)|\[Y/n\]|\[y/N\]|press any key|continue\?|password:|passphrase|"
    r"are you sure|overwrite\?|enter to continue)\s*$",
    re.IGNORECASE,
)
HEAD_LINES = 40
TAIL_LINES = 120

# Capture limits per stream (bytes kept in memory while a command runs).
CAPTURE_HEAD_LINES = 1000
CAPTURE_HEAD_BYTES = 256 * 1024
CAPTURE_TAIL_LINES = 4000
CAPTURE_TAIL_BYTES = 1024 * 1024
UNREAD_BYTES = 256 * 1024
MAX_LINE_BYTES = 64 * 1024
PIPE_GRACE_SECONDS = 2.0
LOGIN_ENV_TIMEOUT = 5.0

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
    # Nothing leaves this machine except the chat itself: telemetry, usage
    # analytics, and update checks of the tools crit and the model run are off.
    "DO_NOT_TRACK": "1",
    "POWERSHELL_UPDATECHECK": "Off",
    "DOTNET_NOLOGO": "1",
    "npm_config_audit": "false",
    "npm_config_fund": "false",
    "npm_config_update_notifier": "false",
    "YARN_ENABLE_TELEMETRY": "0",
    "NEXT_TELEMETRY_DISABLED": "1",
    "NUXT_TELEMETRY_DISABLED": "1",
    "GATSBY_TELEMETRY_DISABLED": "1",
    "STORYBOOK_DISABLE_TELEMETRY": "1",
    "HOMEBREW_NO_ANALYTICS": "1",
    "AZURE_CORE_COLLECT_TELEMETRY": "0",
    "SAM_CLI_TELEMETRY": "0",
    "VCPKG_DISABLE_METRICS": "1",
    "CHECKPOINT_DISABLE": "1",
    "FLUTTER_SUPPRESS_ANALYTICS": "true",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}

SHELL_NAMES = ("auto", "bash", "sh", "zsh", "pwsh", "powershell", "cmd")


@dataclass(frozen=True)
class Shell:
    kind: str  # "bash" | "sh" | "zsh" | "pwsh" | "powershell" | "cmd" | "gitbash"
    exe: str  # full path when detected
    label: str

    @property
    def is_powershell(self) -> bool:
        return self.kind in {"pwsh", "powershell"}

    @property
    def is_posix(self) -> bool:
        return self.kind in {"bash", "sh", "zsh", "gitbash"}

    @property
    def name(self) -> str:
        """The user-facing name passed as ``"shell"`` to ``run_command``."""
        return "bash" if self.kind == "gitbash" else self.kind


# --------------------------------------------------------------------------- detection


def _which_any(find: Callable[[str], str | None], names: tuple[str, ...]) -> str | None:
    for name in names:
        found = find(name)
        if found:
            return found
    return None


def _env_get(env: Any, name: str) -> str:
    value = env.get(name)
    if value is None:
        lowered = name.lower()
        for key, item in env.items():
            if key.lower() == lowered:
                value = item
                break
    return str(value or "")


def _is_excluded_windows_bash(path: str, environ: Any) -> bool:
    """WSL launchers: System32\\bash.exe and the WindowsApps alias."""
    lowered = ntpath.normcase(ntpath.normpath(path))
    root = _env_get(environ, "SystemRoot") or _env_get(environ, "windir") or r"C:\Windows"
    for folder in ("System32", "Sysnative", "SysWOW64"):
        prefix = ntpath.normcase(ntpath.join(root, folder)) + "\\"
        if lowered.startswith(prefix):
            return True
    return "\\windowsapps\\" in lowered


def find_git_bash(
    *,
    environ: Any = None,
    which: Callable[[str], str | None] | None = None,
    is_file: Callable[[str], bool] | None = None,
    run: Callable[[list[str]], str] | None = None,
) -> str | None:
    """Git for Windows' ``bash.exe``, never WSL's ``System32\\bash.exe``.

    Looks next to ``git.exe`` (``Git\\cmd\\git.exe`` -> ``Git\\bin\\bash.exe``),
    in the usual install folders, then asks ``git --exec-path`` (scoop and
    other shims), then accepts a ``bash.exe`` on PATH that is not WSL's.
    """
    env = os.environ if environ is None else environ
    find = which or shutil.which
    exists = is_file or os.path.isfile
    candidates: list[str] = []
    git = _which_any(find, ("git.exe", "git"))
    if git:
        parents = list(PureWindowsPath(git).parents)
        for parent in parents[:3]:
            candidates.append(str(parent / "bin" / "bash.exe"))
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = _env_get(env, var)
        if base:
            candidates.append(str(PureWindowsPath(base) / "Git" / "bin" / "bash.exe"))
    local = _env_get(env, "LOCALAPPDATA")
    if local:
        candidates.append(str(PureWindowsPath(local) / "Programs" / "Git" / "bin" / "bash.exe"))
    for candidate in candidates:
        if not _is_excluded_windows_bash(candidate, env) and exists(candidate):
            return candidate
    if git:
        runner = run or _run_text
        try:
            exec_path = runner([git, "--exec-path"]).strip()
        except Exception:  # noqa: BLE001 - detection must never fail
            exec_path = ""
        if exec_path:
            # <root>/mingw64/libexec/git-core -> <root>/bin/bash.exe
            parents = list(PureWindowsPath(exec_path.replace("/", "\\")).parents)
            for parent in parents[1:4]:
                candidate = str(parent / "bin" / "bash.exe")
                if not _is_excluded_windows_bash(candidate, env) and exists(candidate):
                    return candidate
    bash = find("bash.exe")
    if bash and bash.lower().endswith(".exe") and not _is_excluded_windows_bash(bash, env) and exists(bash):
        return bash
    return None


def _run_text(argv: list[str]) -> str:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(argv, capture_output=True, check=False, timeout=5, stdin=subprocess.DEVNULL, **kwargs)
    return decode(proc.stdout)


def _windows_shells(find: Callable[[str], str | None], environ: Any, git_bash: Callable[[], str | None]) -> dict[str, Shell]:
    shells: dict[str, Shell] = {}
    pwsh = _which_any(find, ("pwsh.exe", "pwsh"))
    if pwsh:
        shells["pwsh"] = Shell("pwsh", pwsh, "PowerShell 7 (pwsh.exe)")
    powershell = _which_any(find, ("powershell.exe", "powershell"))
    if not powershell:
        root = _env_get(environ, "SystemRoot") or r"C:\Windows"
        powershell = str(PureWindowsPath(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    shells["powershell"] = Shell("powershell", powershell, "Windows PowerShell 5.1 (powershell.exe)")
    cmd = _which_any(find, ("cmd.exe", "cmd")) or _env_get(environ, "ComSpec") or "cmd.exe"
    shells["cmd"] = Shell("cmd", cmd, "Command Prompt (cmd.exe)")
    bash = git_bash()
    if bash:
        shells["bash"] = Shell("gitbash", bash, "Git Bash (bash.exe)")
    return shells


def _posix_shells(find: Callable[[str], str | None]) -> dict[str, Shell]:
    shells: dict[str, Shell] = {}
    for name in ("bash", "sh", "zsh"):
        found = find(name)
        if found:
            shells[name] = Shell(name, found, name)
    pwsh = find("pwsh")
    if pwsh:
        shells["pwsh"] = Shell("pwsh", pwsh, "PowerShell 7 (pwsh)")
    if "sh" not in shells and "bash" not in shells:
        shells["sh"] = Shell("sh", "/bin/sh", "sh")
    return shells


def available_shells(
    platform_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
    *,
    environ: Any = None,
    git_bash: Callable[[], str | None] | None = None,
) -> dict[str, Shell]:
    """Installed shells keyed by the name ``run_command`` accepts.

    Windows keys: ``pwsh`` (if installed), ``powershell``, ``cmd``, ``bash``
    (Git Bash, only when installed). POSIX keys: ``bash``, ``sh``, ``zsh``,
    ``pwsh`` as found. Cached for the process when called with defaults.
    """
    if platform_name is None and which is None and environ is None and git_bash is None:
        return dict(_available_cached(sys.platform))
    plat = platform_name if platform_name is not None else sys.platform
    find = which or shutil.which
    env = os.environ if environ is None else environ
    if plat == "win32":
        if git_bash is None:
            native = which is None and sys.platform == "win32"
            git_bash = (lambda: find_git_bash(environ=env, which=find)) if native else (lambda: None)
        return _windows_shells(find, env, git_bash)
    return _posix_shells(find)


@functools.lru_cache(maxsize=None)
def _available_cached(plat: str) -> dict[str, Shell]:
    if plat == "win32":
        return _windows_shells(shutil.which, os.environ, lambda: find_git_bash())
    return _posix_shells(shutil.which)


def detect_shell(
    platform_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
    preference: str | None = None,
) -> Shell:
    """The default shell for ``run_command``.

    ``preference`` is the settings value: ``"auto"`` (default), ``"bash"``,
    ``"pwsh"``, ``"powershell"``, ``"cmd"``, ``"sh"`` or ``"zsh"``. A preferred
    shell that is not installed falls back to auto. Auto is pwsh, then Windows
    PowerShell on Windows; bash, then sh elsewhere (zsh only when preferred).
    """
    plat = platform_name if platform_name is not None else sys.platform
    if which is None and platform_name in (None, sys.platform):
        shells = available_shells()
    else:
        shells = available_shells(plat, which or shutil.which, git_bash=(lambda: None) if which is not None else None)
    wanted = (preference or "auto").strip().lower()
    if wanted in {"git-bash", "gitbash"}:
        wanted = "bash"
    if wanted and wanted != "auto" and wanted in shells:
        return shells[wanted]
    order = ("pwsh", "powershell") if plat == "win32" else ("bash", "sh")
    for name in order:
        if name in shells:
            return shells[name]
    if plat == "win32":
        return Shell("powershell", "powershell.exe", "Windows PowerShell 5.1 (powershell.exe)")
    return Shell("sh", "/bin/sh", "sh")


def _is_windows_shell(shell: Shell) -> bool:
    if shell.kind in {"powershell", "cmd", "gitbash"}:
        return True
    return shell.exe.lower().endswith(".exe") or (sys.platform == "win32")


def shell_preamble(shell: Shell, available: dict[str, Shell] | None = None) -> str:
    """The first lines of the system prompt: the default shell and how to use the others."""
    lines = [f"SHELL: {shell.label}. run_command runs here unless you pass \"shell\"."]
    if shell.kind == "powershell":
        lines.append(
            "Windows PowerShell 5.1: chain with ; and if ($?) { ... }; a top-level && or || is converted for you. "
            "Use PowerShell cmdlets (Get-ChildItem, Select-String, Get-Content), not grep, sed, awk, export, or ls -la."
        )
    elif shell.kind == "pwsh":
        lines.append("&& and || work. Use PowerShell cmdlets (Get-ChildItem, Select-String, Get-Content), not grep, sed, or awk.")
    elif shell.kind == "cmd":
        lines.append("cmd.exe batch syntax: chain with &&; use %% in for loops.")
    others = [name for name, item in (available or {}).items() if item != shell and name != shell.name]
    if others:
        described = ", ".join(
            f'"{name}" ({available[name].label})' for name in others  # type: ignore[index]
        )
        lines.append(f"Other shells for one call: \"shell\": {described}.")
    lines.append(
        "The working folder persists between calls: cd carries over, and cwd sets where one call starts."
    )
    lines.append(
        "For servers and watchers pass \"background\": true, then read with command_output and stop with kill_command."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- PowerShell text scanning


def _ps_mask(text: str) -> str:
    """``text`` with string, here-string and comment contents blanked (same length)."""
    out = list(text)
    n = len(text)

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] not in "\r\n":
                out[k] = " "

    i = 0
    while i < n:
        c = text[i]
        if c == "`":
            i += 2
            continue
        if c == "@" and i + 1 < n and text[i + 1] in "'\"":
            quote = text[i + 1]
            line_end = text.find("\n", i + 2)
            if not text[i + 2 : n if line_end < 0 else line_end].strip():
                match = re.compile(r"\n[ \t]*" + re.escape(quote) + "@").search(text, i + 2)
                if match:
                    blank(i + 2, match.start() + 1)
                    i = match.end()
                else:
                    blank(i + 2, n)
                    i = n
                continue
        if c == "'" or c in "\u2018\u2019":
            j = i + 1
            while j < n:
                if text[j] in "'\u2018\u2019":
                    if j + 1 < n and text[j + 1] in "'\u2018\u2019":
                        j += 2
                        continue
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
            continue
        if c == '"' or c in "\u201c\u201d":
            j = i + 1
            while j < n:
                if text[j] == "`":
                    j += 2
                    continue
                if text[j] in '"\u201c\u201d':
                    if j + 1 < n and text[j + 1] in '"\u201c\u201d':
                        j += 2
                        continue
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
            continue
        if text.startswith("<#", i):
            j = text.find("#>", i + 2)
            end = n if j < 0 else j + 2
            blank(i, end)
            i = end
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\r\n;|({}&"):
            j = text.find("\n", i)
            end = n if j < 0 else j
            blank(i, end)
            i = end
            continue
        i += 1
    return "".join(out)


def _ps_blank_hashtables(masked: str) -> str:
    """``masked`` with the contents of ``@{ ... }`` hashtable literals blanked (same length).

    ``@{a=1; b=2}`` holds keys, not commands, so ``a=1 b`` must not look like
    bash's ``VAR=value cmd``.
    """
    out = list(masked)
    i = 0
    n = len(masked)
    while i < n:
        if masked.startswith("@{", i) and (i == 0 or masked[i - 1] != "`"):
            depth = 0
            j = i + 1
            while j < n:
                if masked[j] == "{":
                    depth += 1
                elif masked[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            for k in range(i + 2, min(j, n)):
                if out[k] not in "\r\n":
                    out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out)


@dataclass
class _Statement:
    start: int
    end: int
    ops: list[tuple[int, str]] = field(default_factory=list)


def _ps_statements(text: str, masked: str | None = None) -> tuple[list[_Statement], bool]:
    """Top-level statements and their top-level ``&&``/``||``; second value: nested chain seen."""
    masked = _ps_mask(text) if masked is None else masked
    statements: list[_Statement] = []
    nested = False
    depth = 0
    start = 0
    current = _Statement(0, 0)
    i = 0
    n = len(masked)
    while i < n:
        c = masked[i]
        if c == "`":
            i += 2
            continue
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth = max(0, depth - 1)
        elif masked.startswith("&&", i) or masked.startswith("||", i):
            if depth == 0:
                current.ops.append((i, masked[i : i + 2]))
            else:
                nested = True
            i += 2
            continue
        elif depth == 0 and (c == ";" or c == "\n"):
            if c == "\n":
                before = masked[start:i].rstrip()
                if before.endswith(("|", "&&", "||")) or not before.strip():
                    if not before.strip():
                        start = i + 1
                        current = _Statement(start, start)
                    i += 1
                    continue
            current.start, current.end = start, i
            if masked[start:i].strip():
                statements.append(current)
            start = i + 1
            current = _Statement(start, start)
        i += 1
    current.start, current.end = start, n
    if masked[start:n].strip():
        statements.append(current)
    return statements, nested


def rewrite_and_or(command: str) -> str:
    """Rewrite top-level ``a && b`` / ``a || b`` for Windows PowerShell 5.1.

    Each chain becomes plain statements that track the status the way
    PowerShell 7 and bash do (left to right, short-circuit)::

        $global:LASTEXITCODE = $null; a
        $__critS = __crit_status $?
        if ($__critS -eq 0) { $global:LASTEXITCODE = $null; b
        $__critS = __crit_status $? }
        __crit_finish $__critS

    ``__crit_status``/``__crit_finish`` are defined by the run wrapper;
    ``__crit_finish`` leaves the chain status in ``$LASTEXITCODE`` so the chain
    reports it as its exit code. Text without a top-level chain is returned
    unchanged. A chain inside a block raises ``ValueError``.
    """
    masked = _ps_mask(command)
    statements, nested = _ps_statements(command, masked)
    if nested:
        raise ValueError("&& or || inside a block")
    if not any(statement.ops for statement in statements):
        return command
    pieces: list[str] = []
    cursor = 0
    for statement in statements:
        if not statement.ops:
            continue
        raw = command[statement.start : statement.end]
        pieces.append(command[cursor : statement.start] + raw[: len(raw) - len(raw.lstrip())])
        bounds = [statement.start] + [pos for pos, _ in statement.ops] + [statement.end]
        ops = [op for _, op in statement.ops]
        segments = []
        for index in range(len(bounds) - 1):
            begin = bounds[index] + (2 if index else 0)
            segments.append(command[begin : bounds[index + 1]].strip())
        if any(not segment for segment in segments):
            raise ValueError("&& or || without a command on one side")
        lines = [f"$global:LASTEXITCODE = $null; {segments[0]}", "$__critS = __crit_status $?"]
        for op, segment in zip(ops, segments[1:]):
            test = "-eq" if op == "&&" else "-ne"
            lines.append(f"if ($__critS {test} 0) {{ $global:LASTEXITCODE = $null; {segment}")
            lines.append("$__critS = __crit_status $? }")
        lines.append("__crit_finish $__critS")
        pieces.append("\n".join(lines))
        cursor = statement.end
    pieces.append(command[cursor:])
    return "".join(pieces)


# --------------------------------------------------------------------------- lint

_CMD_POS = r"(?:^\s*|[;|&{(]\s*|\n\s*)"
_PS_FORMS = {
    "grep": "Select-String -Pattern <text> -Path <files> (or search_code)",
    "sed": "(Get-Content f) -replace 'a','b' | Set-Content f (or edit_file)",
    "awk": "ForEach-Object / -split",
    "head": "Get-Content <file> -TotalCount 20 (or Select-Object -First 20)",
    "tail": "Get-Content <file> -Tail 20 (or Select-Object -Last 20)",
    "xargs": "ForEach-Object { ... }",
    "touch": "New-Item -ItemType File <path>",
    "which": "Get-Command <name>",
}
# (pattern, problem, PowerShell form, Windows only)
_BASH_RULES: tuple[tuple[re.Pattern[str], str, str, bool], ...] = (
    (re.compile(_CMD_POS + r"export\s+\w+="), "export is bash", "$env:NAME = 'value'", False),
    (re.compile(_CMD_POS + r"[A-Za-z_][A-Za-z0-9_]*=[^\s=]*[ \t]+[A-Za-z.\\/]"), "VAR=value cmd is bash", "$env:VAR = 'value'; cmd", False),
    (re.compile(r"<<"), "a heredoc (<<EOF) is bash", "write the file with write_files, or a here-string @' ... '@", False),
    (re.compile(_CMD_POS + r"source\s+\S"), "source is bash", ". .\\.venv\\Scripts\\Activate.ps1 (dot-source)", False),
    (re.compile(_CMD_POS + r"ls\s+-[a-zA-Z]+"), "ls flags are bash", "Get-ChildItem -Force (or list_files)", True),
    (re.compile(_CMD_POS + r"rm\s+-[rRf]+"), "rm -rf is bash", "Remove-Item -Recurse -Force <path>", True),
    (re.compile(_CMD_POS + r"mkdir\s+-p\b"), "mkdir -p is bash", "New-Item -ItemType Directory -Force <path>", True),
    (re.compile(_CMD_POS + r"cat\s*>"), "cat > file is bash", "write_files, or Set-Content", True),
    (re.compile(r"(?<![\w$])/dev/null\b"), "/dev/null is bash", "$null or | Out-Null", True),
)


@functools.lru_cache(maxsize=None)
def _command_exists(name: str) -> bool:
    found = shutil.which(name)
    if not found:
        return False
    return "\\windowsapps\\" not in found.lower()


def lint_command(
    command: str,
    shell: Shell,
    available: dict[str, Shell] | None = None,
    *,
    has_command: Callable[[str], bool] | None = None,
) -> str | None:
    """A precise message when the command uses syntax this shell cannot run.

    Only PowerShell is linted. Text inside quotes, here-strings and comments
    is ignored, and so are the keys of ``@{...}`` hashtables. On Windows PowerShell 5.1 a top-level ``&&``/``||`` is
    rewritten when the command runs, so only a chain inside a block is
    refused. When Git Bash is installed, bash-only commands are answered with
    a hint to resend with ``"shell": "bash"``.
    """
    if not shell.is_powershell:
        return None
    windows = _is_windows_shell(shell)
    if available is None:
        available = available_shells() if sys.platform == "win32" else {}
    bash = available.get("bash") if windows else None
    masked = _ps_blank_hashtables(_ps_mask(command))
    if shell.kind == "powershell":
        try:
            rewrite_and_or(command)
        except ValueError:
            return (
                f"not run: && and || inside a block are not Windows PowerShell 5.1 syntax. "
                f"Use: cmd1; if ($?) {{ cmd2 }}. Shell is {shell.label}."
            )
    problem = ""
    form = ""
    if windows:
        tools = re.search(_CMD_POS + r"(grep|sed|awk|xargs|head|tail|touch|which)(?=\s|$)", masked)
        if tools:
            name = tools.group(1)
            problem, form = f"{name} is a Unix tool", _PS_FORMS[name]
    if not problem:
        for pattern, message, replacement, windows_only in _BASH_RULES:
            if windows_only and not windows:
                continue
            if pattern.search(masked):
                problem, form = message, replacement
                break
    if not problem and windows and re.search(r"(?<![\w.\\/-])python3(?![\w.-])", masked):
        exists = has_command or _command_exists
        if not exists("python3"):
            problem, form = "python3 is not installed here", "python, py, or the venv python shown in ENVIRONMENT"
    if not problem:
        return None
    if bash is not None:
        return (
            f"not run: {problem} and the shell is {shell.label}. "
            f'Resend the same command with "shell": "bash" to run it in {bash.label}, '
            f"or use the PowerShell form: {form}."
        )
    return f"not run: {problem}. Use: {form}. Shell is {shell.label}."


# --------------------------------------------------------------------------- environment

_SECRET_NAME_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|(^|_)PAT$|SESSION_KEY|CLIENT_KEY",
    re.IGNORECASE,
)
_SECRET_PREFIXES = ("CRITIQUE_", "OPENAI_", "ANTHROPIC_", "AWS_SECRET", "AZURE_OPENAI_", "GEMINI_", "GOOGLE_API")
_SECRET_NAMES = frozenset({"CI_JOB_TOKEN", "GITLAB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN", "AWS_SESSION_TOKEN"})


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return upper in _SECRET_NAMES or upper.startswith(_SECRET_PREFIXES) or bool(_SECRET_NAME_RE.search(upper))


def scrubbed_environment(extra: dict[str, str | None] | None = None, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` (or ``base``) without secrets, then ``extra``.

    Names containing TOKEN, SECRET, PASSWORD, PASSWD, API_KEY, ACCESS_KEY,
    PRIVATE_KEY or CREDENTIAL, and ``CRITIQUE_*``, ``OPENAI_*``,
    ``ANTHROPIC_*``, ``AWS_SECRET*`` are removed. ``extra`` is applied after
    scrubbing, so settings can pass a variable back on purpose; a ``None``
    value removes a variable.
    """
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items() if not is_secret_name(key)}
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


_TOOLCHAIN_CACHE: dict[tuple[str, str, str, str], dict[str, str]] = {}
_TOOLCHAIN_LOCK = threading.Lock()


def _toolchain(env: dict[str, str]) -> dict[str, str]:
    """JDK/Gradle/Android adjustments for ``env``; the scans run once per process."""
    key = tuple(str(env.get(name) or "") for name in ("JAVA_HOME", "PATH", "ANDROID_HOME", "ANDROID_SDK_ROOT"))
    with _TOOLCHAIN_LOCK:
        cached = _TOOLCHAIN_CACHE.get(key)  # type: ignore[arg-type]
    if cached is not None:
        return cached
    probe = dict(env)
    _prefer_compiler(probe)
    _prefer_gradle(probe)
    sdk = android_sdk(probe, scan=True)
    if sdk and not str(probe.get("ANDROID_HOME") or "").strip():
        probe["ANDROID_HOME"] = sdk
    if sdk and not str(probe.get("ANDROID_SDK_ROOT") or "").strip():
        probe["ANDROID_SDK_ROOT"] = sdk
    changes = {name: probe[name] for name in ("JAVA_HOME", "PATH", "ANDROID_HOME", "ANDROID_SDK_ROOT") if probe.get(name) != env.get(name) and name in probe}
    with _TOOLCHAIN_LOCK:
        _TOOLCHAIN_CACHE[key] = changes  # type: ignore[index]
    return changes


def environment(extra: dict[str, str] | None = None, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """The child environment: scrubbed, quiet, plus JDK/Gradle/Android helpers."""
    env = scrubbed_environment(base=base)
    env.update(QUIET_ENV)
    if extra:
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
    env.update(_toolchain(env))
    return env


_LOGIN_ENV: dict[str, str] | None = None
_LOGIN_DONE = threading.Event()
_LOGIN_LOCK = threading.Lock()
_LOGIN_STARTED = False
_LOGIN_SKIP = frozenset({"_", "SHLVL", "PWD", "OLDPWD", "PS1", "PS2", "PROMPT_COMMAND", "TERM", "COLUMNS", "LINES"})
_LOGIN_SENTINEL = "__CRIT_ENV_START__"


def _capture_login_env() -> None:
    global _LOGIN_ENV
    try:
        if sys.platform == "win32" or os.environ.get("CRIT_SHELL_LOGIN_ENV", "1") == "0":
            return
        candidate = os.environ.get("SHELL", "")
        if not (candidate and os.path.basename(candidate) in {"bash", "zsh", "sh", "dash", "ksh"} and os.access(candidate, os.X_OK)):
            candidate = shutil.which("bash") or "/bin/sh"
        script = f"printf '\\000{_LOGIN_SENTINEL}\\000'; env -0"
        proc = subprocess.Popen(
            [candidate, "-l", "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=LOGIN_ENV_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc, grace=0)
            return
        text = out.decode("utf-8", "replace")
        marker = f"\0{_LOGIN_SENTINEL}\0"
        if proc.returncode != 0 or marker not in text:
            return
        env: dict[str, str] = {}
        for item in text.split(marker, 1)[1].split("\0"):
            if "=" in item:
                key, value = item.split("=", 1)
                if key and key not in _LOGIN_SKIP:
                    env[key] = value
        _LOGIN_ENV = env
    except Exception:  # noqa: BLE001 - optional, falls back silently
        _LOGIN_ENV = None
    finally:
        _LOGIN_DONE.set()


def prewarm_login_environment() -> None:
    """Start capturing the login-shell environment in the background (once)."""
    global _LOGIN_STARTED
    with _LOGIN_LOCK:
        if _LOGIN_STARTED:
            return
        _LOGIN_STARTED = True
    threading.Thread(target=_capture_login_env, name="crit-login-env", daemon=True).start()


def login_environment(timeout: float = LOGIN_ENV_TIMEOUT + 1) -> dict[str, str] | None:
    """The login shell's environment, captured once per process (None on Windows or failure)."""
    if sys.platform == "win32":
        return None
    prewarm_login_environment()
    _LOGIN_DONE.wait(timeout)
    return _LOGIN_ENV


def merge_login_environment(base: dict[str, str], login: dict[str, str] | None) -> dict[str, str]:
    """``base`` plus variables only the login shell sets; PATH entries are appended."""
    merged = dict(base)
    for key, value in (login or {}).items():
        if key == "PATH":
            have = merged.get("PATH", "").split(os.pathsep) if merged.get("PATH") else []
            extra = [part for part in value.split(os.pathsep) if part and part not in have]
            merged["PATH"] = os.pathsep.join(have + extra)
        elif key not in merged:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- JDK / Gradle / Android


def _javac_name(platform_name: str) -> str:
    return "javac.exe" if platform_name == "win32" else "javac"


def _has_javac(home: Path, platform_name: str) -> bool:
    return (home / "bin" / _javac_name(platform_name)).is_file()


def compiler_home(
    environ: dict[str, str] | None = None,
    platform_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> Path | None:
    """A JDK root that contains javac.

    ``JAVA_HOME`` wins when it already has a compiler. A home that exists but
    has no javac is a JRE, so a compiler found on PATH or under the usual JDK
    folders is used instead.
    """
    plat = platform_name if platform_name is not None else sys.platform
    env = os.environ if environ is None else environ
    find = which or shutil.which
    raw = str(env.get("JAVA_HOME") or "").strip()
    if raw:
        home = Path(raw)
        if _has_javac(home, plat):
            return home
        if not home.is_dir():
            return None
    found = find(_javac_name(plat))
    if found:
        binary = Path(found)
        if binary.parent.name.lower() == "bin" and _has_javac(binary.parent.parent, plat):
            return binary.parent.parent
    if which is not None:
        return None
    return _scan_jdk_roots(plat)


def _scan_jdk_roots(plat: str) -> Path | None:
    found = installed_jdks(plat)
    return found[0].home if found else None


@dataclass(frozen=True)
class Jdk:
    home: Path
    version: int  # major: 8, 11, 17, 21
    source: str = ""


def jdk_version(home: Path) -> int:
    """The major version of the JDK at ``home`` from its ``release`` file (or its folder name); 0 if unknown."""
    try:
        text = (Path(home) / "release").read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^JAVA_VERSION="?(\d+)(?:\.(\d+))?', text, re.M)
        if match:
            major = int(match.group(1))
            return int(match.group(2) or 0) if major == 1 else major
    except OSError:
        pass
    name = Path(home).name.lower()
    match = re.search(r"(?:jdk|java|openjdk|temurin|corretto|zulu|jbr)[-_]?(?:1\.)?(\d{1,2})(?!\d)", name) or re.match(
        r"^(?:1\.)?(\d{1,2})[.\-_]", name
    )
    return int(match.group(1)) if match else 0


def _jdk_roots(plat: str, environ: Any) -> list[tuple[Path, str, int]]:
    """``(folder, label, depth)``: where JDKs are installed on ``plat``. ``depth`` 1 = children are JDKs."""
    home = Path.home()
    roots: list[tuple[Path, str, int]] = []
    if plat == "win32":
        bases = []
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            value = _env_get(environ, key)
            if value and Path(value) not in bases:
                bases.append(Path(value))
        if not bases:
            bases = [Path("C:\\Program Files")]
        local = _env_get(environ, "LOCALAPPDATA")
        for base in bases:
            for vendor in ("Java", "Eclipse Adoptium", "Eclipse Foundation", "AdoptOpenJDK", "Microsoft", "Zulu",
                           "Amazon Corretto", "BellSoft", "Semeru", "OpenJDK", "RedHat", "Oracle", "Android\\jdk"):
                roots.append((base / vendor, vendor, 1))
            roots.append((base / "Android" / "Android Studio" / "jbr", "Android Studio", 0))
            roots.append((base / "Android" / "Android Studio" / "jre", "Android Studio", 0))
        if local:
            roots.append((Path(local) / "Programs" / "Android Studio" / "jbr", "Android Studio", 0))
            roots.append((Path(local) / "Programs" / "Eclipse Adoptium", "Eclipse Adoptium", 1))
            roots.append((Path(local) / "JetBrains" / "Toolbox" / "apps" / "AndroidStudio" / "ch-0", "Android Studio", 2))
    elif plat == "darwin":
        roots.append((Path("/Library/Java/JavaVirtualMachines"), "", 1))
        roots.append((home / "Library" / "Java" / "JavaVirtualMachines", "", 1))
        roots.append((Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home"), "Android Studio", 0))
        roots.append((Path("/Applications/Android Studio.app/Contents/jre/Contents/Home"), "Android Studio", 0))
    else:
        roots.append((Path("/usr/lib/jvm"), "", 1))
        roots.append((Path("/usr/java"), "", 1))
        roots.append((Path("/opt/android-studio/jbr"), "Android Studio", 0))
        roots.append((home / "android-studio" / "jbr", "Android Studio", 0))
        roots.append((Path("/snap/android-studio/current/jbr"), "Android Studio", 0))
        roots.append((Path("/opt"), "", 1))
    roots.append((home / ".gradle" / "jdks", "Gradle", 1))
    roots.append((home / ".jdks", "IntelliJ", 1))
    roots.append((home / ".sdkman" / "candidates" / "java", "SDKMAN", 1))
    return roots


def _jdk_homes(folder: Path, depth: int, plat: str) -> list[Path]:
    """JDK homes at ``folder`` (depth 0), its children (1), or grandchildren (2), macOS bundles included."""
    if not folder.is_dir():
        return []
    if depth == 0:
        return [folder] if _has_javac(folder, plat) else []
    found: list[Path] = []
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return []
    for child in children[:80]:
        for candidate in (child, child / "Contents" / "Home", child / "jbr", child / "jbr" / "Contents" / "Home"):
            if _has_javac(candidate, plat):
                found.append(candidate)
                break
        else:
            if depth > 1 and child.is_dir():
                found.extend(_jdk_homes(child, depth - 1, plat))
            elif child.is_dir() and depth == 1:
                try:
                    nested = sorted(child.iterdir())[:10]
                except OSError:
                    nested = []
                found.extend(item for item in nested if _has_javac(item, plat))
    return found


@functools.lru_cache(maxsize=8)
def installed_jdks(plat: str | None = None) -> tuple[Jdk, ...]:
    """Every JDK (with javac) in the usual places, newest first; Android Studio's own JDK included."""
    plat = plat if plat is not None else sys.platform
    seen: set[str] = set()
    found: list[Jdk] = []
    for folder, label, depth in _jdk_roots(plat, os.environ):
        for home in _jdk_homes(folder, depth, plat):
            try:
                key = os.path.normcase(str(home.resolve()))
            except OSError:
                key = os.path.normcase(str(home))
            if key in seen:
                continue
            seen.add(key)
            source = label or ""
            lowered = str(home).lower()
            if "android studio" in lowered or "android-studio" in lowered or "androidstudio" in lowered:
                source = "Android Studio"
            found.append(Jdk(home, jdk_version(home), source))
    found.sort(key=lambda jdk: -jdk.version)
    return tuple(found)


# Newest Java each Gradle version can run on (Gradle's compatibility matrix).
_GRADLE_MAX_JAVA = ((5, 0, 11), (5, 4, 12), (6, 0, 13), (6, 3, 14), (6, 7, 15), (7, 0, 16), (7, 3, 17), (7, 5, 18),
                    (7, 6, 19), (8, 3, 20), (8, 5, 21), (8, 8, 22), (8, 10, 23), (8, 14, 24))


def _version_pair(text: str) -> tuple[int, int] | None:
    match = re.match(r"\s*(\d+)\.(\d+)", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def agp_version(root: Path) -> tuple[int, int] | None:
    """The Android Gradle plugin version the project asks for (from build files or the version catalog)."""
    root = Path(root)
    texts: list[str] = []
    for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradle/libs.versions.toml"):
        try:
            texts.append((root / name).read_text(encoding="utf-8", errors="replace")[:200_000])
        except OSError:
            continue
    joined = "\n".join(texts)
    patterns = (
        r"com\.android\.tools\.build:gradle:(\d+\.\d+)",
        r"""id\s*\(?\s*["']com\.android\.(?:application|library|test|dynamic-feature)["']\s*\)?\s*version\s*["'](\d+\.\d+)""",
        r"""(?im)^\s*(?:agp|androidGradlePlugin|android[-_]gradle[-_]plugin|android[-_]gradle|androidPlugin|android-agp)\s*=\s*["'](\d+\.\d+)""",
    )
    for pattern in patterns:
        match = re.search(pattern, joined)
        if match:
            return _version_pair(match.group(1))
    return None


def gradle_version(root: Path) -> tuple[int, int] | None:
    url = _wrapper_distribution(Path(root) / "gradle" / "wrapper" / "gradle-wrapper.properties")
    match = re.search(r"gradle-(\d+\.\d+)", url)
    return _version_pair(match.group(1)) if match else None


def project_java_range(root: Path) -> tuple[int, int | None, str]:
    """``(lowest, highest or None, why)`` for the JDK that runs this project's Gradle build."""
    low, why = 0, ""
    agp = agp_version(root)
    if agp is not None:
        low = 17 if agp >= (8, 0) else 11 if agp >= (7, 0) else 8
        why = f"AGP {agp[0]}.{agp[1]} needs JDK {low}+"
    elif _is_android_project(Path(root)):
        low, why = 17, "Android projects need JDK 17+"
    high: int | None = None
    gradle = gradle_version(root)
    if gradle is not None:
        high = 8 if gradle < (5, 0) else max(java for major, minor, java in _GRADLE_MAX_JAVA if gradle >= (major, minor))
        why = (why + "; " if why else "") + f"Gradle {gradle[0]}.{gradle[1]} runs on JDK {high} at most"
    return low, high, why


def choose_jdk(environ: dict[str, str], root: Path, plat: str | None = None) -> tuple[Jdk | None, str]:
    """The JDK to run this project's build with, and a note when it differs from JAVA_HOME.

    JAVA_HOME wins when it has javac and fits the project (AGP 8 needs 17+,
    the wrapper's Gradle caps the newest). Otherwise Android Studio's own JDK
    when it fits, else the oldest installed JDK that fits (older Gradle
    versions break on new Java).
    """
    plat = plat if plat is not None else sys.platform
    low, high, why = project_java_range(root)
    raw = str(environ.get("JAVA_HOME") or "").strip()
    current: Jdk | None = None
    if raw and _has_javac(Path(raw), plat):
        current = Jdk(Path(raw), jdk_version(Path(raw)), "JAVA_HOME")

    def fits(jdk: Jdk) -> bool:
        if jdk.version == 0:
            return low == 0
        return jdk.version >= low and (high is None or jdk.version <= high)

    if current is not None and (fits(current) or (low == 0 and high is None)):
        return current, ""
    if low == 0:
        # Not an Android build: JAVA_HOME, else javac on PATH, as before (see _prefer_compiler).
        return current, ""
    candidates = [jdk for jdk in installed_jdks(plat) if fits(jdk)]
    if not candidates:
        if current is not None:
            return current, (f"JAVA_HOME is JDK {current.version} but {why}; no fitting JDK is installed" if why else "")
        found = installed_jdks(plat)
        return (found[0] if found else None), (f"{why}; no fitting JDK is installed" if why and low else "")
    studio = [jdk for jdk in candidates if jdk.source == "Android Studio"]
    chosen = studio[0] if studio else sorted(candidates, key=lambda jdk: jdk.version)[0]
    if current is not None:
        return chosen, f"JAVA_HOME is JDK {current.version} but {why}; commands use JDK {chosen.version}"
    return chosen, ""


def project_toolchain(env: dict[str, str], root: Path, plat: str | None = None) -> dict[str, str]:
    """``env`` with JAVA_HOME (and PATH) set to the project's JDK and ANDROID_HOME to its SDK."""
    plat = plat if plat is not None else sys.platform
    out = dict(env)
    try:
        jdk, _note = choose_jdk(out, root, plat)
    except Exception:  # noqa: BLE001 - a broken scan must not stop a command
        jdk = None
    if jdk is not None and str(out.get("JAVA_HOME") or "") != str(jdk.home):
        out["JAVA_HOME"] = str(jdk.home)
        out["PATH"] = str(jdk.home / "bin") + os.pathsep + out.get("PATH", "")
    sdk = android_sdk(out, scan=True, workspace=root)
    if sdk:
        out.setdefault("ANDROID_HOME", sdk)
        if not str(out.get("ANDROID_HOME") or "").strip() or not Path(out["ANDROID_HOME"]).is_dir():
            out["ANDROID_HOME"] = sdk
        if not str(out.get("ANDROID_SDK_ROOT") or "").strip() or not Path(out["ANDROID_SDK_ROOT"]).is_dir():
            out["ANDROID_SDK_ROOT"] = sdk
    return out


def cached_gradle() -> Path | None:
    """An unpacked Gradle binary from a previous wrapper download, if one exists."""
    root = Path.home() / ".gradle" / "wrapper" / "dists"
    if not root.is_dir():
        return None
    found = [path for path in root.glob("gradle-*-bin/*/gradle-*/bin/gradle") if path.is_file() and os.access(path, os.X_OK)]
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_mtime)


def _prefer_gradle(env: dict[str, str]) -> None:
    """Put a cached Gradle on PATH when the command name is not already installed."""
    if shutil.which("gradle", path=env.get("PATH")):
        return
    binary = cached_gradle()
    if binary is None:
        return
    env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")


def android_sdk(environ: dict[str, str], *, scan: bool, workspace: Path | None = None) -> str:
    """The Android SDK path: ANDROID_HOME, ANDROID_SDK_ROOT, ``sdk.dir`` in local.properties, then a scan.

    The scan looks where Android Studio installs it: ``%LOCALAPPDATA%\\Android\\Sdk``
    on Windows, ``~/Library/Android/sdk`` on macOS, ``~/Android/Sdk`` on Linux.
    A variable naming a folder that does not exist is passed over when a scan
    finds a real one.
    """
    named = ""
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        raw = str(environ.get(key) or "").strip()
        if raw:
            if not scan or Path(raw).is_dir():
                return raw
            named = named or raw
    if not scan:
        return ""
    if workspace is not None:
        local = _local_properties_sdk(Path(workspace))
        if local:
            return local
    for candidate in _sdk_candidates(environ):
        if (candidate / "platforms").is_dir() or (candidate / "platform-tools").is_dir():
            return str(candidate)
    return named


def _local_properties_sdk(root: Path) -> str:
    try:
        text = (root / "local.properties").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"^\s*sdk\.dir\s*[=:]\s*(.+?)\s*$", text, re.M)
    if not match:
        return ""
    value = re.sub(r"\\(.)", r"\1", match.group(1))
    return value if Path(value).is_dir() else ""


def _sdk_candidates(environ: Any) -> list[Path]:
    home = Path.home()
    candidates: list[Path] = []
    local = _env_get(environ, "LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Android" / "Sdk")
    candidates += [home / "AppData" / "Local" / "Android" / "Sdk", home / "Library" / "Android" / "sdk",
                   home / "Android" / "Sdk", home / "Android" / "sdk"]
    if sys.platform == "win32":
        candidates += [Path("C:\\Android\\Sdk"), Path("C:\\Android\\sdk"),
                       Path("C:\\Program Files (x86)\\Android\\android-sdk")]
    else:
        candidates += [Path("/opt/android-sdk"), Path("/usr/lib/android-sdk"), Path("/opt/android/sdk")]
    return candidates


def _prefer_compiler(env: dict[str, str], platform_name: str | None = None) -> None:
    """Point JAVA_HOME at a JDK when the current one cannot compile."""
    plat = platform_name if platform_name is not None else sys.platform
    raw = str(env.get("JAVA_HOME") or "").strip()
    if raw and not Path(raw).is_dir():
        return
    if raw and _has_javac(Path(raw), plat):
        return
    home = compiler_home(env, plat)
    if home is not None:
        env["JAVA_HOME"] = str(home)


_GRADLEW_RE = re.compile(
    r"(?P<lead>(?:^|[;&|({\n]|\bcall)\s*(?:&\s*)?)"
    r"(?P<quote>['\"]?)(?P<exe>(?:[^\s'\";&|()]*[\\/])?gradlew(?:\.bat)?)(?P=quote)(?=\s|$|[;&|)])",
    re.IGNORECASE,
)


_BARE_WRAPPER_RE = re.compile(
    r"(?P<lead>(?:^|[;&|({\n])\s*(?:&\s*)?)(?P<exe>(?:\./)?gradlew(?:\.bat)?)(?=\s|$|[;&|)])", re.IGNORECASE
)


def adjust_command(command: str, shell: "Shell | None" = None, cwd: Path | None = None) -> str:
    """Make a gradle wrapper call run as intended in ``shell``.

    Adds ``--console=plain``. On Windows, PowerShell runs a file in the current
    folder only with a ``.\\`` prefix and cmd does not understand ``./``, so a
    bare ``gradlew`` or ``./gradlew`` becomes ``.\\gradlew.bat`` (``gradlew.bat``
    in cmd) when that file is in ``cwd``.
    """
    text = command
    if shell is not None and cwd is not None and _is_windows_shell(shell) and (Path(cwd) / "gradlew.bat").is_file():
        replacement = ".\\gradlew.bat" if shell.is_powershell else "gradlew.bat"
        if shell.is_powershell or shell.kind == "cmd":
            text = _BARE_WRAPPER_RE.sub(lambda m: m.group("lead") + replacement, text)
    if "--console" in text:
        return text
    return _GRADLEW_RE.sub(lambda m: m.group(0) + " --console=plain", text)


def tool_hints(
    workspace: Path,
    platform_name: str | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Interpreters and wrappers the model should use, for the ENVIRONMENT block.

    Gradle is detected with ``which`` and the wrapper files only. Running
    ``gradle -v`` can download a distribution, so this never does that.
    """
    plat = platform_name if platform_name is not None else sys.platform
    find = which or shutil.which
    env = os.environ if environ is None else environ
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
    wrapper = _wrapper_command(root, plat)
    if wrapper:
        line = f"gradle wrapper: {wrapper} (use this; do not download Gradle)"
        pinned = _wrapper_distribution(root / "gradle" / "wrapper" / "gradle-wrapper.properties")
        if pinned:
            line += f"; pinned {pinned}"
        hints.append(line)
    gradle_names = ("gradle.bat", "gradle.cmd", "gradle") if plat == "win32" else ("gradle",)
    gradle_path = _which_first(find, gradle_names)
    if gradle_path:
        hint = f"gradle on PATH: {gradle_path}"
        if not wrapper:
            hint += " (no project wrapper; use this)"
        hints.append(hint)
    elif which is None:
        cached = cached_gradle()
        if cached is not None:
            hints.append(
                f"gradle binary: {cached} (not on PATH; commands can run gradle. "
                "Use it to create the wrapper. Do not download another copy)"
            )
        else:
            hints.append("gradle on PATH: not installed")
    else:
        hints.append("gradle on PATH: not installed")
    java_home = str(env.get("JAVA_HOME") or "").strip()
    compiler = compiler_home(env, plat, which)
    if which is None and environ is None:
        try:
            chosen, note = choose_jdk(dict(env), root, plat)
        except Exception:  # noqa: BLE001
            chosen, note = None, ""
        if chosen is not None and (note or str(chosen.home) != java_home):
            version = f"JDK {chosen.version}, " if chosen.version else ""
            source = f"{chosen.source}, " if chosen.source and chosen.source != "JAVA_HOME" else ""
            hints.append(
                f"java: JAVA_HOME={chosen.home} ({version}{source}run_command uses it"
                + (f"; {note}" if note else "") + ")"
            )
            java_home = ""
            compiler = None
            others = [jdk for jdk in installed_jdks(plat) if jdk.home != chosen.home][:3]
            if others:
                hints.append("other JDKs: " + "; ".join(f"JDK {jdk.version}: {jdk.home}" for jdk in others))
    if not any(line.startswith("java:") for line in hints) and java_home and compiler is not None and not _has_javac(Path(java_home), plat):
        hints.append(
            f"java: JAVA_HOME={compiler} "
            f"(JAVA_HOME={java_home} has no javac; commands use this JDK)"
        )
    elif any(line.startswith("java:") for line in hints):
        pass
    elif java_home:
        hints.append(f"java: JAVA_HOME={java_home}")
    elif compiler is not None:
        hints.append(f"java: JAVA_HOME={compiler}")
    else:
        java_names = ("java.exe", "java") if plat == "win32" else ("java",)
        java_path = _which_first(find, java_names)
        hints.append(f"java: {java_path}" if java_path else "java: not installed")
    sdk = android_sdk(env, scan=environ is None, workspace=root if environ is None else None)
    if sdk:
        hints.append(f"android sdk: {sdk}" + ("" if str(env.get("ANDROID_HOME") or "") == sdk else " (run_command sets ANDROID_HOME to it)"))
    else:
        hints.append("android sdk: not found (ANDROID_HOME unset, no local.properties sdk.dir, nothing in the default folder)")
    if _is_android_project(root):
        hints.append(
            "android project: yes. Sync with the gradle wrapper before COMPLETED. "
            "Do not download a Gradle distribution into the repo."
        )
    if (root / "package.json").is_file():
        hints.append("node: npm (package.json present)")
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        hints.append("python project: pyproject.toml present")
    return hints


def _which_first(find: Callable[[str], str | None], names: tuple[str, ...]) -> str | None:
    return _which_any(find, names)


def _wrapper_command(root: Path, plat: str) -> str:
    bat = (root / "gradlew.bat").is_file()
    script = (root / "gradlew").is_file()
    if plat == "win32" and bat:
        return ".\\gradlew.bat"
    if script:
        return "./gradlew"
    if bat:
        return ".\\gradlew.bat"
    return ""


def _wrapper_distribution(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("distributionUrl="):
            return line.split("=", 1)[1].strip().replace("\\:", ":").replace("\\=", "=")
    return ""


def _is_android_project(root: Path) -> bool:
    if not (root / "settings.gradle").is_file() and not (root / "settings.gradle.kts").is_file():
        return False
    manifests = (
        root / "AndroidManifest.xml",
        root / "app" / "src" / "main" / "AndroidManifest.xml",
        root / "src" / "main" / "AndroidManifest.xml",
    )
    if any(path.is_file() for path in manifests) or any(root.glob("*/src/main/AndroidManifest.xml")):
        return True
    builds = [root / "build.gradle", root / "build.gradle.kts", root / "app" / "build.gradle", root / "app" / "build.gradle.kts"]
    builds.extend(root.glob("*/build.gradle"))
    builds.extend(root.glob("*/build.gradle.kts"))
    return any(_mentions_android_plugin(path) for path in builds)


def _mentions_android_plugin(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return False
    return "com.android" in text


# --------------------------------------------------------------------------- scripts

_PS_PRELUDE = r"""$ProgressPreference = 'SilentlyContinue'
$ConfirmPreference = 'None'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
if (Test-Path -Path variable:PSNativeCommandUseErrorActionPreference) { $PSNativeCommandUseErrorActionPreference = $false }
if (Test-Path -Path variable:PSStyle) { try { $PSStyle.OutputRendering = 'PlainText' } catch { } }
function __crit_status([bool]$ok) { if ($null -ne $global:LASTEXITCODE) { [int]$global:LASTEXITCODE } elseif ($ok) { 0 } else { 1 } }
function __crit_finish([int]$code) { $global:LASTEXITCODE = $code }
"""

_PS_BODY = r"""$__critCode = 0
try {
    $__critText = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{encoded}'))
    $__critParseErrors = $null
    $__critTokens = $null
    $__critAst = [System.Management.Automation.Language.Parser]::ParseInput($__critText, [ref]$__critTokens, [ref]$__critParseErrors)
    $__critHead = ''
    $__critLast = $__critText
    $__critPlain = $false
    $__critNamed = (-not $__critParseErrors) -and ($null -ne $__critAst.BeginBlock -or $null -ne $__critAst.ProcessBlock -or $null -ne $__critAst.DynamicParamBlock -or $null -ne $__critAst.CleanBlock -or ($null -ne $__critAst.EndBlock -and -not $__critAst.EndBlock.Unnamed))
    if ((-not $__critParseErrors) -and (-not $__critNamed) -and $null -eq $__critAst.ParamBlock -and $null -eq $__critAst.BeginBlock -and $null -eq $__critAst.ProcessBlock -and $null -eq $__critAst.DynamicParamBlock -and $null -ne $__critAst.EndBlock -and $null -eq $__critAst.EndBlock.Traps -and $__critAst.EndBlock.Statements.Count -gt 0) {
        $__critStatements = $__critAst.EndBlock.Statements
        $__critFinal = $__critStatements[$__critStatements.Count - 1]
        $__critHead = $__critText.Substring(0, $__critFinal.Extent.StartOffset)
        $__critLast = $__critFinal.Extent.Text
        $__critPlain = $__critFinal -is [System.Management.Automation.Language.PipelineAst]
    }
    if ($__critHead.Trim()) { . ([scriptblock]::Create($__critHead)) }
    $global:LASTEXITCODE = $null
    $__critErrorCount = $global:Error.Count
    $__critErrorTop = $null
    if ($global:Error.Count -gt 0) { $__critErrorTop = $global:Error[0] }
    $__critOk = $true
    if ($__critNamed) {
        $__critBlock = [scriptblock]::Create($__critLast)
        . $__critBlock
        $__critOk = $?
    } else {
        $__critBlock = [scriptblock]::Create($__critLast + "`n`$__critOk = `$?")
        . $__critBlock
    }
    $__critNewError = $__critPlain -and (($global:Error.Count -gt $__critErrorCount) -or ($global:Error.Count -gt 0 -and -not [object]::ReferenceEquals($global:Error[0], $__critErrorTop)))
    if ($null -ne $global:LASTEXITCODE) { $__critCode = [int]$global:LASTEXITCODE }
    elseif ((-not $__critOk) -or $__critNewError) { $__critCode = 1 }
} catch {
    try { [Console]::Error.WriteLine(($_ | Out-String).TrimEnd()) } catch { }
    $__critCode = 1
} finally {
{marker}}
if ($Host.Name -eq 'ConsoleHost') { $Host.SetShouldExit($__critCode) } else { exit $__critCode }
"""

_PS_MARKER = r"""    try {
        $__critLocation = Get-Location
        if ($__critLocation.Provider.Name -eq 'FileSystem') { [System.IO.File]::WriteAllText('{path}', $__critLocation.ProviderPath) }
    } catch { }
"""


def _ps_literal(text: str) -> str:
    return text.replace("'", "''")


def powershell_script(command: str, *, kind: str = "pwsh", marker: str | None = None) -> str:
    """The wrapper that runs ``command`` in PowerShell and sets an honest exit code.

    Exit code semantics (both PowerShell 7 and 5.1):

    * ``exit N`` anywhere exits with N.
    * A terminating error (``throw``, ``-ErrorAction Stop``, parse error)
      prints the error to stderr and exits 1.
    * Otherwise only the LAST top-level statement decides: ``$LASTEXITCODE``
      is reset to ``$null`` right before it; if a native program (or a
      ``.ps1`` that calls ``exit``) ran in it, its exit code is the result.
      Else the result is 1 when ``$?`` was false right after it, or when it
      is a plain pipeline that added records to ``$Error`` (non-terminating
      cmdlet errors), and 0 otherwise. Control statements (``try``/``catch``,
      ``if``, a PowerShell 7 ``a || b`` chain) are judged by ``$?`` only, so
      a handled error does not fail the command. So ``git bad; Get-Item .`` exits 0 and ``Get-Item missing``
      exits 1, like bash would.
    * A script whose top level is named blocks (``param(...)``, ``begin``,
      ``process``, ``end``) runs whole and is judged by ``$LASTEXITCODE``
      and ``$?``.
    * A 5.1 ``a && b`` / ``a || b`` chain is rewritten (see
      :func:`rewrite_and_or`) and exits with the chain's status.

    The wrapper ends with ``$Host.SetShouldExit(code)`` rather than ``exit``:
    ``exit`` discards a table that Out-Default is still formatting (the output
    of ``Get-ChildItem`` or ``Get-Location`` would vanish).

    ``$ErrorActionPreference`` stays ``Continue``: setting it to ``Stop``
    would turn a native program's stderr into a terminating error on 5.1.
    The command runs dot-sourced at script scope, split into "everything but
    the last statement" and "the last statement" with the PowerShell parser.
    """
    if kind == "powershell":
        try:
            command = rewrite_and_or(command)
        except ValueError:
            pass
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    marker_text = _PS_MARKER.replace("{path}", _ps_literal(marker)) if marker else ""
    return _PS_PRELUDE + _PS_BODY.replace("{encoded}", encoded).replace("{marker}", marker_text)


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def posix_script(command: str, *, kind: str = "bash", marker: str | None = None) -> str:
    """The script for bash/sh/zsh/Git Bash. An EXIT trap records the final folder."""
    body = command.replace("\r\n", "\n").rstrip() + "\n"
    if not marker:
        return body
    where = "{ pwd -W 2>/dev/null || pwd -P; }" if kind == "gitbash" else "pwd -P"
    return (
        "__crit_done() { __crit_rc=$?; " + where + " > " + _sh_quote(marker) + " 2>/dev/null; exit $__crit_rc; }\n"
        "trap __crit_done EXIT\n" + body
    )


def cmd_script(command: str, *, marker: str | None = None) -> str:
    """The batch file for cmd.exe (UTF-8 code page; batch rules such as %% apply)."""
    body = command.replace("\r\n", "\n").rstrip().split("\n")
    lines = ["@echo off", "chcp 65001>nul", *body, ""]
    if marker:
        lines += ['@set "__CRIT_RC=%ERRORLEVEL%"', f'@cd > "{marker}" 2>nul', "@exit /b %__CRIT_RC%"]
    return "\r\n".join(lines) + "\r\n"


def _script(shell: Shell, command: str, marker: str | None) -> tuple[str, bytes]:
    if shell.is_powershell:
        return ".ps1", ("\ufeff" + powershell_script(command, kind=shell.kind, marker=marker)).encode("utf-8")
    if shell.kind == "cmd":
        return ".cmd", cmd_script(command, marker=marker).encode("utf-8")
    return ".sh", posix_script(command, kind=shell.kind, marker=marker).encode("utf-8")


# Reads the wrapper and runs it as a script block. A Group Policy execution
# policy (AllSigned, Restricted) overrides -ExecutionPolicy and blocks -File,
# but not -EncodedCommand or a script block made from text.
_PS_BOOTSTRAP = "& ([scriptblock]::Create([System.IO.File]::ReadAllText('{path}')))"


def script_argv(shell: Shell, path: str) -> list[str]:
    """argv that runs a script file written by :func:`_script`.

    PowerShell gets a short ``-EncodedCommand`` bootstrap that reads the file,
    so the run does not depend on the execution policy. ``-ExecutionPolicy
    Bypass`` stays for the ``.ps1`` files the command itself calls
    (``npm.ps1``, ``Activate.ps1``). ``-OutputFormat Text`` keeps stderr from
    being sent as CLIXML.
    """
    if shell.is_powershell:
        bootstrap = _PS_BOOTSTRAP.replace("{path}", _ps_literal(path))
        encoded = base64.b64encode(bootstrap.encode("utf-16-le")).decode("ascii")
        return [shell.exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text", "-EncodedCommand", encoded]
    if shell.kind == "cmd":
        return [shell.exe, "/d", "/c", "call", path]
    if shell.kind in {"bash", "gitbash"}:
        return [shell.exe, "--noprofile", "--norc", path.replace("\\", "/") if shell.kind == "gitbash" else path]
    if shell.kind == "zsh":
        return [shell.exe, "-f", path]
    return [shell.exe, path]


def command_argv(
    command: str,
    *,
    platform_name: str | None = None,
    shell: Shell | None = None,
    cwd: Path | None = None,
) -> list[str]:
    """An inline argv for one command (no script file).

    Kept for callers that pass a fake ``runner``; real runs use script files
    (see :class:`ShellSession`), which have no command-line length limit.
    """
    chosen = shell or detect_shell(platform_name)
    if chosen.is_powershell:
        text = command
        if cwd is not None:
            text = f"Set-Location -LiteralPath '{_ps_literal(str(cwd))}'\n" + command
        script = powershell_script(text, kind=chosen.kind)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [chosen.exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    if chosen.kind == "cmd":
        return [chosen.exe, "/d", "/s", "/c", command]
    if cwd is not None:
        command = f"cd {_sh_quote(str(cwd))} && {command}"
    return [chosen.exe, "-c", command]


_PRIVATE_DIR: str | None = None
_PRIVATE_LOCK = threading.Lock()


def _private_dir() -> str:
    global _PRIVATE_DIR
    with _PRIVATE_LOCK:
        if _PRIVATE_DIR is None or not os.path.isdir(_PRIVATE_DIR):
            _PRIVATE_DIR = tempfile.mkdtemp(prefix="crit-shell-")
        return _PRIVATE_DIR


def _remove(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- output capture


class _Capture:
    """Bounded output of one stream: first lines, last lines, and a dropped count."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.head: list[bytes] = []
        self.head_bytes = 0
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_bytes = 0
        self.dropped = 0
        self.pending = b""
        self.unread = bytearray()
        self.unread_dropped = 0
        self.total = 0

    def feed(self, data: bytes) -> None:
        with self.lock:
            self.total += len(data)
            self.unread += data
            if len(self.unread) > UNREAD_BYTES:
                cut = len(self.unread) - UNREAD_BYTES
                newline = self.unread.find(b"\n", cut)
                cut = newline + 1 if 0 <= newline < cut + 4096 else cut
                del self.unread[:cut]
                self.unread_dropped += cut
            parts = (self.pending + data).split(b"\n")
            self.pending = parts.pop()
            for part in parts:
                self._line(part + b"\n")
            while len(self.pending) > MAX_LINE_BYTES:
                self._line(self.pending[:MAX_LINE_BYTES])
                self.pending = self.pending[MAX_LINE_BYTES:]

    def _line(self, line: bytes) -> None:
        if len(self.head) < CAPTURE_HEAD_LINES and self.head_bytes + len(line) <= CAPTURE_HEAD_BYTES and not self.tail:
            self.head.append(line)
            self.head_bytes += len(line)
            return
        self.tail.append(line)
        self.tail_bytes += len(line)
        while len(self.tail) > CAPTURE_TAIL_LINES or self.tail_bytes > CAPTURE_TAIL_BYTES:
            gone = self.tail.popleft()
            self.tail_bytes -= len(gone)
            self.dropped += 1

    def take_unread(self, final: bool = False) -> tuple[bytes, int]:
        """New bytes since the last call (whole lines unless ``final``) and bytes skipped."""
        with self.lock:
            if final:
                cut = len(self.unread)
            else:
                cut = self.unread.rfind(b"\n") + 1
                if cut == 0 and len(self.unread) > 4096:
                    cut = len(self.unread)
            data = bytes(self.unread[:cut])
            del self.unread[:cut]
            skipped, self.unread_dropped = self.unread_dropped, 0
            return data, skipped

    def text(self) -> str:
        with self.lock:
            head = b"".join(self.head)
            tail = b"".join(self.tail) + self.pending
            dropped = self.dropped
        encoding = detect_encoding(head + tail)
        if not dropped:
            return _decode_as(head + tail, encoding)
        return _decode_as(head, encoding) + f"... {dropped} lines omitted ...\n" + _decode_as(tail, encoding)


def _pump(pipe: Any, capture: _Capture, wake: threading.Event) -> None:
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            capture.feed(chunk)
            wake.set()
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass
        wake.set()


def _kill_process_tree(proc: subprocess.Popen, *, grace: float = 1.0) -> None:
    """Stop ``proc`` and everything it started."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        if grace > 0:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def kill_tree(proc: subprocess.Popen) -> None:
    _kill_process_tree(proc, grace=0)


class _Launch:
    """One running script: process, reader threads, temp files."""

    def __init__(self, shell: Shell, command: str, cwd: Path, env: dict[str, str], *, want_cwd: bool) -> None:
        self.shell = shell
        self.command = command
        folder = _private_dir()
        token = uuid.uuid4().hex
        self.marker = os.path.join(folder, f"{token}.cwd") if want_cwd else None
        suffix, data = _script(shell, adjust_command(command, shell, cwd), self.marker)
        self.script = os.path.join(folder, f"{token}{suffix}")
        with open(self.script, "wb") as handle:
            handle.write(data)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.stdout = _Capture()
        self.stderr = _Capture()
        self.wake = threading.Event()
        try:
            self.proc = subprocess.Popen(
                script_argv(shell, self.script),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
                **kwargs,
            )
        except BaseException:
            self.cleanup()
            raise
        self.readers = [
            threading.Thread(target=_pump, args=(self.proc.stdout, self.stdout, self.wake), daemon=True),
            threading.Thread(target=_pump, args=(self.proc.stderr, self.stderr, self.wake), daemon=True),
        ]
        for reader in self.readers:
            reader.start()
        self.killed = False
        self.ended: float | None = None

    def poll(self) -> int | None:
        code = self.proc.poll()
        if code is not None and self.ended is None:
            self.ended = time.monotonic()
        return code

    def kill(self) -> None:
        self.killed = True
        _kill_process_tree(self.proc)

    def join_readers(self, grace: float = PIPE_GRACE_SECONDS) -> None:
        deadline = time.monotonic() + grace
        for reader in self.readers:
            reader.join(max(0.0, deadline - time.monotonic()))

    def exit_code(self) -> int | None:
        if self.killed:
            return None
        code = self.proc.returncode
        if code is None:
            return None
        if code < 0 and sys.platform != "win32":
            return 128 - code
        return code

    def final_cwd(self) -> Path | None:
        if not self.marker:
            return None
        try:
            with open(self.marker, "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        text = decode(raw).strip().strip("\ufeff").strip()
        if not text:
            return None
        if self.shell.kind == "gitbash":
            match = re.match(r"^/([a-zA-Z])(/.*)?$", text)
            if match:
                text = f"{match.group(1).upper()}:{(match.group(2) or '/')}"
        path = Path(text)
        return path if path.is_dir() else None

    def cleanup(self) -> None:
        _remove(getattr(self, "script", None))
        _remove(getattr(self, "marker", None))


@dataclass
class CommandResult:
    exit_code: int | None  # None = timed out / killed
    stdout: str  # decoded, tidied, bounded (head+tail kept)
    stderr: str
    seconds: float
    cwd: Path  # cwd AFTER the command
    timed_out: bool
    interrupted: bool
    shell: Shell
    #: Set when a timed-out command was left running as this background job instead of being killed.
    job_id: str | None = None


def _execute(
    shell: Shell,
    command: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    on_output: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    want_cwd: bool = False,
    detach_on_timeout: bool = False,
) -> tuple[_Launch, bool, bool]:
    """Run until exit, timeout, or cancel. With ``detach_on_timeout`` a command that is
    still producing work at the deadline is left running (the caller adopts it)."""
    launch = _Launch(shell, command, cwd, env, want_cwd=want_cwd)
    timed_out = interrupted = False
    deadline = launch.started + max(0.0, timeout)
    last_flush = 0.0

    def flush(final: bool = False) -> None:
        if on_output is None:
            return
        parts = []
        for capture in (launch.stdout, launch.stderr):
            data, skipped = capture.take_unread(final)
            if skipped:
                parts.append(f"... {skipped} bytes skipped ...\n")
            if data:
                parts.append(_decode_as(data, detect_encoding(data)))
        text = "".join(parts)
        if text:
            try:
                on_output(text)
            except Exception:  # noqa: BLE001 - UI problems never stop a command
                pass

    try:
        while True:
            try:
                launch.proc.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
            if launch.poll() is not None:
                break
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                interrupted = True
                launch.kill()
                break
            if now >= deadline:
                timed_out = True
                if detach_on_timeout and not _launch_waits_for_input(launch):
                    flush(final=True)
                    return launch, timed_out, interrupted
                launch.kill()
                break
            if now - last_flush >= 0.1:
                flush()
                last_flush = now
        launch.join_readers()
        flush(final=True)
    except BaseException:
        launch.kill()
        launch.cleanup()
        raise
    return launch, timed_out, interrupted


def _launch_waits_for_input(launch: _Launch) -> bool:
    try:
        text = launch.stdout.text()[-2000:] + "\n" + launch.stderr.text()[-2000:]
    except Exception:  # noqa: BLE001
        return False
    return waiting_for_input(tidy(text))


# --------------------------------------------------------------------------- session


_SESSIONS: "weakref.WeakSet[ShellSession]" = weakref.WeakSet()


@atexit.register
def _close_all_sessions() -> None:
    for session in list(_SESSIONS):
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    if _PRIVATE_DIR:
        shutil.rmtree(_PRIVATE_DIR, ignore_errors=True)


@dataclass
class _Job:
    id: str
    command: str
    launch: _Launch
    shell: Shell
    exit_code: int | None = None
    done: bool = False


class ShellSession:
    """A terminal-like session: persistent cwd, per-call shell choice, background jobs.

    ``run`` raises ``ValueError`` for a shell name that is not available or a
    ``cwd`` that is not a folder; the message is suitable for the model.
    """

    def __init__(
        self,
        workspace: Path,
        shell: Shell | None = None,
        *,
        available: dict[str, Shell] | None = None,
        env_extra: dict[str, str | None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.shell = shell or detect_shell()
        self.available = dict(available) if available is not None else available_shells()
        self.env_extra = dict(env_extra or {})
        self.cwd = self.workspace
        self._env: dict[str, str] | None = None
        self._jobs: dict[str, _Job] = {}
        self._counter = 0
        self._lock = threading.Lock()
        #: One entry per foreground command: command, shell, exit, seconds, cwd, and how it ended.
        self.history: list[dict[str, Any]] = []
        #: A foreground command still running at its timeout becomes a background job instead of being killed.
        self.detach_on_timeout = True
        if self.shell.is_posix and sys.platform != "win32":
            prewarm_login_environment()
        _SESSIONS.add(self)

    # -- helpers

    def environment(self) -> dict[str, str]:
        if self._env is None:
            base = dict(os.environ)
            if sys.platform != "win32":
                base = merge_login_environment(base, login_environment())
            env = environment(self.env_extra, base=base)
            if "JAVA_HOME" not in self.env_extra:
                env = project_toolchain(env, self.workspace)
            self._env = env
        return self._env

    def resolve(self, name: str | None) -> Shell:
        """The shell for a ``"shell"`` argument (None/auto/default = the session shell)."""
        if not name:
            return self.shell
        key = str(name).strip().lower()
        key = {
            "default": "auto", "": "auto", "git-bash": "bash", "gitbash": "bash", "git bash": "bash",
            "bash.exe": "bash", "powershell.exe": "powershell", "windows powershell": "powershell",
            "pwsh.exe": "pwsh", "powershell7": "pwsh", "cmd.exe": "cmd",
        }.get(key, key)
        if key == "auto" or key == self.shell.name:
            return self.shell
        if key in self.available:
            return self.available[key]
        if key == "powershell" and "pwsh" in self.available:
            return self.available["pwsh"]
        if key == "pwsh" and "powershell" in self.available:
            return self.available["powershell"]
        names = ", ".join(sorted(set(self.available) | {self.shell.name}))
        raise ValueError(f'shell "{name}" is not available here. Available: {names}')

    def _start_dir(self, cwd: Path | str | None) -> Path:
        if cwd is not None and str(cwd).strip():
            path = Path(cwd)
            if not path.is_absolute():
                path = self.cwd / path
            if not path.is_dir():
                raise ValueError(f"cwd is not a folder: {cwd}")
            return path
        if not self.cwd.is_dir():
            self.cwd = self.workspace
        return self.cwd

    # -- foreground

    def run(
        self,
        command: str,
        *,
        timeout: float,
        shell: str | None = None,
        cwd: Path | None = None,
        on_output: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        """Run one command. ``cwd`` sets where it starts; the folder it ends in becomes the session cwd."""
        chosen = self.resolve(shell)
        start = self._start_dir(cwd)
        launch, timed_out, interrupted = _execute(
            chosen,
            command,
            cwd=start,
            env=self.environment(),
            timeout=timeout,
            on_output=on_output,
            cancel=cancel,
            want_cwd=True,
            detach_on_timeout=self.detach_on_timeout,
        )
        if timed_out and launch.poll() is None and not launch.killed:
            job_id = self._adopt(command, launch, chosen)
            result = CommandResult(
                exit_code=None,
                stdout=tidy(launch.stdout.text()),
                stderr=tidy(launch.stderr.text()),
                seconds=time.monotonic() - launch.started,
                cwd=self.cwd,
                timed_out=True,
                interrupted=False,
                shell=chosen,
                job_id=job_id,
            )
            self._remember(command, result, start)
            return result
        try:
            final = launch.final_cwd()
            if final is not None:
                try:
                    same = os.path.samefile(final, self.cwd)
                except OSError:
                    same = False
                if not same:
                    self.cwd = final
            elif cwd is not None:
                self.cwd = start
            result = CommandResult(
                exit_code=launch.exit_code(),
                stdout=tidy(launch.stdout.text()),
                stderr=tidy(launch.stderr.text()),
                seconds=time.monotonic() - launch.started,
                cwd=self.cwd,
                timed_out=timed_out,
                interrupted=interrupted,
                shell=chosen,
            )
            self._remember(command, result, start)
            return result
        finally:
            launch.cleanup()

    def _adopt(self, command: str, launch: _Launch, shell: Shell) -> str:
        """Keep a still-running foreground command as a background job."""
        with self._lock:
            self._counter += 1
            job_id = f"b{self._counter}"
            self._jobs[job_id] = _Job(job_id, command, launch, shell)
        return job_id

    def _remember(self, command: str, result: CommandResult, start: Path) -> None:
        if result.interrupted:
            how = "interrupted"
        elif result.job_id:
            how = f"moved to background {result.job_id}"
        elif result.timed_out:
            how = "timed out"
        else:
            how = f"exit {result.exit_code}"
        entry = {
            "command": command,
            "shell": result.shell.label,
            "exit": result.exit_code,
            "seconds": round(result.seconds, 1),
            "cwd": str(start),
            "result": how,
            "at": time.time(),
        }
        with self._lock:
            self.history.append(entry)
            del self.history[:-200]

    def note_retry(self, reason: str) -> None:
        """Mark the last history entry as retried (the next entry is the retry)."""
        with self._lock:
            if self.history:
                self.history[-1]["retried"] = reason

    # -- background

    def start_background(self, command: str, *, shell: str | None = None, cwd: Path | None = None) -> str:
        """Start a long-running command; returns its job id (``b1``, ``b2``...)."""
        chosen = self.resolve(shell)
        start = self._start_dir(cwd)
        launch = _Launch(chosen, command, start, self.environment(), want_cwd=False)
        with self._lock:
            self._counter += 1
            job_id = f"b{self._counter}"
            self._jobs[job_id] = _Job(job_id, command, launch, chosen)
        return job_id

    def _job(self, job_id: str) -> _Job:
        job = self._jobs.get(str(job_id).strip())
        if job is None:
            known = ", ".join(self._jobs) or "none"
            raise KeyError(f"no background job {job_id} (jobs: {known})")
        return job

    def _reap(self, job: _Job) -> None:
        if job.done:
            return
        if job.launch.poll() is not None:
            job.launch.join_readers(0.5)
            job.exit_code = job.launch.exit_code()
            job.done = True
            job.launch.cleanup()

    def read_background(self, job_id: str, *, wait: float = 0.0) -> tuple[str, bool, int | None]:
        """New output since the last read, whether it still runs, and its exit code."""
        job = self._job(job_id)
        launch = job.launch
        deadline = time.monotonic() + max(0.0, wait)
        while not job.done:
            self._reap(job)
            if job.done or time.monotonic() >= deadline:
                break
            if launch.stdout.unread or launch.stderr.unread:
                if b"\n" in launch.stdout.unread or b"\n" in launch.stderr.unread:
                    break
            launch.wake.wait(min(0.1, max(0.0, deadline - time.monotonic())))
            launch.wake.clear()
        parts = []
        for capture in (launch.stdout, launch.stderr):
            data, skipped = capture.take_unread(final=job.done)
            if skipped:
                parts.append(f"... {skipped} bytes skipped ...\n")
            if data:
                parts.append(_decode_as(data, detect_encoding(data)))
        return tidy("".join(parts)), not job.done, job.exit_code

    def kill_background(self, job_id: str) -> bool:
        """Stop a background job and its children. True when it was still running."""
        job = self._job(job_id)
        self._reap(job)
        if job.done:
            return False
        job.launch.kill()
        job.launch.join_readers(0.5)
        job.exit_code = None
        job.done = True
        job.launch.cleanup()
        return True

    def jobs(self) -> list[dict]:
        rows = []
        for job in list(self._jobs.values()):
            self._reap(job)
            rows.append(
                {
                    "id": job.id,
                    "command": job.command,
                    "running": not job.done,
                    "exit_code": job.exit_code,
                    "started": job.launch.started_wall,
                    "shell": job.shell.name,
                    "pid": job.launch.proc.pid,
                }
            )
        return rows

    def close(self) -> None:
        """Kill every background job that is still running."""
        for job in list(self._jobs.values()):
            try:
                self._reap(job)
                if not job.done:
                    job.launch.kill()
                    job.done = True
                job.launch.cleanup()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- compatibility


def run(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    shell: Shell | None = None,
    runner: Callable[..., Any] | None = None,
    on_output: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[int | None, str, str, float]:
    """Run one command without a session. Returns exit code (None on timeout), stdout, stderr, seconds."""
    chosen = shell or detect_shell()
    started = time.monotonic()
    if runner is not None:
        argv = command_argv(adjust_command(command), shell=chosen, cwd=Path(cwd))
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
    launch, _timed_out, _interrupted = _execute(
        chosen, command, cwd=Path(cwd), env=environment(), timeout=timeout, on_output=on_output, cancel=cancel
    )
    try:
        return launch.exit_code(), launch.stdout.text(), launch.stderr.text(), time.monotonic() - started
    finally:
        launch.cleanup()


# --------------------------------------------------------------------------- decoding


@functools.lru_cache(maxsize=1)
def _windows_codepages() -> tuple[int, ...]:
    """(OEM, ANSI) code pages of this Windows console session; empty elsewhere."""
    if sys.platform != "win32":
        return ()
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return tuple(cp for cp in (int(kernel32.GetOEMCP()), int(kernel32.GetACP())) if cp)
    except Exception:  # noqa: BLE001
        return ()


def _looks_utf16le(raw: bytes) -> bool:
    sample = raw[:4096]
    if len(sample) < 8:
        return False
    odd = sample[1::2]
    even = sample[0::2]
    return odd.count(0) >= 0.9 * len(odd) and even.count(0) <= 0.1 * len(even)


def detect_encoding(raw: bytes, *, codepages: tuple[int, ...] | None = None, platform_name: str | None = None) -> str:
    """The best encoding for ``raw``: BOM, UTF-16 pattern, UTF-8, OEM, ANSI, then latin-1."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if _looks_utf16le(raw):
        return "utf-16-le"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    plat = platform_name if platform_name is not None else sys.platform
    pages = codepages if codepages is not None else (_windows_codepages() if plat == "win32" else ())
    names = [f"cp{page}" for page in pages if page and page != 65001]
    if plat != "win32" and codepages is None:
        import locale

        names.append(locale.getpreferredencoding(False) or "")
    for name in names:
        if not name or name.lower().replace("-", "") in {"utf8", "cp65001"}:
            continue
        try:
            raw.decode(name)
            return name
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def _decode_as(raw: bytes, encoding: str) -> str:
    try:
        return raw.decode(encoding, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def decode(data: object, *, codepages: tuple[int, ...] | None = None, platform_name: str | None = None) -> str:
    if isinstance(data, str):
        return data
    if not isinstance(data, (bytes, bytearray)):
        return ""
    raw = bytes(data)
    return _decode_as(raw, detect_encoding(raw, codepages=codepages, platform_name=platform_name))


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


# Windows PowerShell 5.1 wraps a native program's first stderr line in an error record:
#   gradlew.bat : warning: ...           <- kept, without the "gradlew.bat : " prefix
#   At line:1 char:1                     <- dropped
#   + .\gradlew.bat assembleDebug 2>&1    <- dropped
#   + ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~     <- dropped
#       + CategoryInfo : NotSpecified: (...) [], RemoteException   <- dropped
#       + FullyQualifiedErrorId : NativeCommandError               <- dropped
_PS_NOISE_RE = re.compile(
    r"^(?:At (?:line:\d+ char:\d+|[^\n]*:\d+ char:\d+)|\+ [~ ]+|\s+\+ (?:CategoryInfo|FullyQualifiedErrorId)\s*:.*)$"
)


def _strip_ps_noise(lines: list[str]) -> list[str]:
    if not any("NativeCommandError" in line or "RemoteException" in line for line in lines):
        return lines
    out: list[str] = []
    skip_echo = False
    for line in lines:
        if _PS_NOISE_RE.match(line):
            skip_echo = line.startswith("At ")
            continue
        if skip_echo and line.startswith("+ "):
            continue
        skip_echo = False
        out.append(re.sub(r"^[\w.\\/:-]+\.(?:bat|cmd|exe) : ", "", line))
    return out


def tidy(text: str) -> str:
    """Strip ANSI, carriage-return progress redraws, PowerShell error decoration, and repeated lines."""
    text = clean_clixml(_ANSI_RE.sub("", text))
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rsplit("\r", 1)[-1]
        out.append(line.rstrip())
    out = _strip_ps_noise(out)
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
