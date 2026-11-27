# SPDX-License-Identifier: Apache-2.0
"""A short picture of the machine and the project, for the model's first turn.

:func:`environment_snapshot` gives the OS, the shell, the working folder, the git branch
and a status summary, the languages in the project, the test command (settings
``check_command``, else a ``Test command:`` line in AGENT.md), the developer tools on
PATH, and the background jobs still running. The engine sends it as the first user turn
(Stage 3), not in the system prompt, so the cached prefix stays stable. Every probe is
bounded in time and never raises: a missing git or a huge tree only shortens the text.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from critique_bot import project_home
from critique_bot.code_index import SKIP_DIR_NAMES

#: Tools worth knowing about, in the order they are listed.
TOOLS = (
    "git", "rg", "python3", "python", "pip", "uv", "node", "npm", "pnpm", "yarn", "java", "javac", "kotlinc",
    "gradle", "mvn", "go", "cargo", "rustc", "gcc", "g++", "clang", "make", "cmake", "ninja", "bazel", "adb",
    "repo", "docker", "ruff", "pytest", "shellcheck", "ktlint", "gofmt",
)
LANGUAGES = {
    ".py": "Python", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C/C++", ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".js": "JavaScript",
    ".mjs": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".rb": "Ruby",
    ".php": "PHP", ".swift": "Swift", ".m": "Objective-C", ".scala": "Scala", ".sh": "Shell", ".bp": "Soong (Android.bp)",
    ".mk": "Make", ".gradle": "Gradle", ".aidl": "AIDL", ".proto": "Protobuf", ".xml": "XML", ".dart": "Dart",
}
#: Files looked at to guess the languages (a git listing, else a walk).
LANGUAGE_SAMPLE = 50_000
PROBE_SECONDS = 3.0
NOTE_FILES = ("AGENT.md", "AGENTS.md", "CLAUDE.md")


def _git(workspace: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=str(workspace), capture_output=True, timeout=PROBE_SECONDS,
                              check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def git_summary(workspace: Path) -> str:
    """"branch X; 3 modified, 1 untracked" (or why there is none)."""
    if shutil.which("git") is None:
        return "git is not installed"
    status = _git(workspace, "status", "--porcelain=v1", "--branch", "--untracked-files=normal")
    if status is None:
        return "not a git repository (or git status took too long)"
    lines = status.splitlines()
    branch = "detached HEAD"
    if lines and lines[0].startswith("## "):
        head = lines[0][3:]
        branch = head.split("...", 1)[0].removeprefix("No commits yet on ")
        if "[" in head:
            branch += " " + head[head.index("[") :]
        lines = lines[1:]
    counts: Counter[str] = Counter()
    for line in lines:
        code = line[:2]
        if code == "??":
            counts["untracked"] += 1
        elif "U" in code or code in {"AA", "DD"}:
            counts["conflicted"] += 1
        elif "A" in code:
            counts["added"] += 1
        elif "D" in code:
            counts["deleted"] += 1
        elif "R" in code:
            counts["renamed"] += 1
        else:
            counts["modified"] += 1
    changes = ", ".join(f"{count} {kind}" for kind, count in counts.most_common()) or "clean"
    return f"branch {branch}; {changes}"


def _sample_files(workspace: Path) -> list[str]:
    listed = _git(workspace, "ls-files")
    if listed:
        return listed.splitlines()[:LANGUAGE_SAMPLE]
    found: list[str] = []
    for folder, dirs, files in os.walk(workspace):
        dirs[:] = [name for name in dirs if name not in SKIP_DIR_NAMES and not name.startswith(".")]
        found.extend(files)
        if len(found) >= 5_000:
            break
    return found


def languages(workspace: Path, limit: int = 6) -> str:
    counts: Counter[str] = Counter()
    for name in _sample_files(workspace):
        language = LANGUAGES.get(os.path.splitext(name)[1].lower())
        if language:
            counts[language] += 1
    if not counts:
        return "none detected"
    return ", ".join(f"{language} ({count:,} files)" for language, count in counts.most_common(limit))


def test_command(workspace: Path, settings: dict[str, Any] | None = None) -> str:
    """The project's test command and where it came from, or ""."""
    configured = (settings or {}).get("check_command")
    if isinstance(configured, str) and configured.strip():
        return f"{configured.strip()} (check_command in .crit/settings.json)"
    places = [project_home.project_dir(workspace, migrate=False) / project_home.NOTES_NAME]
    places += [workspace / name for name in NOTE_FILES]
    for path in places:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        command = project_home.check_command_from_notes(text)
        if command:
            try:
                where = path.relative_to(workspace).as_posix()
            except ValueError:
                where = str(path)
            return f"{command} (from {where})"
    return ""


def tools_on_path(path_value: str | None = None) -> list[str]:
    return [name for name in TOOLS if shutil.which(name, path=path_value) is not None]


def background_jobs(session: Any) -> list[str]:
    """One line per background job of a shell session that is still running."""
    if session is None or not hasattr(session, "jobs"):
        return []
    try:
        rows = session.jobs()
    except Exception:  # noqa: BLE001 (a snapshot never fails over a job listing)
        return []
    out = []
    for row in rows:
        if row.get("running"):
            command = " ".join(str(row.get("command") or "").split())
            out.append(f"{row.get('id')}: {command[:100]}")
    return out


def environment_snapshot(
    workspace: Path,
    *,
    shell: Any = None,
    session: Any = None,
    settings: dict[str, Any] | None = None,
) -> str:
    """The snapshot as text, one fact per line."""
    workspace = Path(workspace).resolve()
    cwd = Path(getattr(session, "cwd", None) or workspace)
    shell = shell or getattr(session, "shell", None)
    shell_text = (f"{getattr(shell, 'label', '') or getattr(shell, 'kind', '')} ({getattr(shell, 'exe', '')})"
                  if shell is not None else os.environ.get("SHELL", "") or "unknown")
    path_value = None
    if session is not None and hasattr(session, "environment"):
        try:
            path_value = session.environment().get("PATH")
        except Exception:  # noqa: BLE001
            path_value = None
    system = f"{platform.system()} {platform.release()} ({platform.machine()})".strip()
    lines = [
        "ENVIRONMENT SNAPSHOT",
        f"os: {system}",
        f"shell: {shell_text}",
        f"cwd: {cwd}",
        f"git: {git_summary(workspace)}",
        f"languages: {languages(workspace)}",
        f"test command: {test_command(workspace, settings) or 'none found (ask, or look for a test script)'}",
        f"tools on PATH: {', '.join(tools_on_path(path_value)) or 'none of the usual ones'}",
    ]
    jobs = background_jobs(session)
    lines.append("background jobs: " + ("; ".join(jobs) if jobs else "none"))
    return "\n".join(lines)
