"""The prompt a session starts with, in a fixed, byte-stable order, and the notes appended later.

The first message of a conversation is the *prefix*. Within a session it never changes,
so a server's prompt cache (llama.cpp, vLLM) reuses it on every request:

1. the shell preamble, the instructions (engine rules) and the role text, with no date,
   git status or other state that changes during a session;
2. the tools block (text tool formats; with native tools the request carries them, sorted
   by name);
3. ``ENVIRONMENT``: facts fixed for the session (OS, shell, workspace, sites, helpers);
4. memory files: ``~/.crit/AGENT.md``, then ``AGENTS.md`` and ``.crit/AGENT.md`` from the
   repository root down to the workspace, deeper files last (they override), capped at
   :data:`MEMORY_CAP` bytes with a warning when cut;
5. (Stage 8) a repo map, refreshed only at compaction.

What does change goes *after* the prefix: :func:`session_context` (date, git status) leads
the first task, and a mode, role or permission change mid-session is appended as a short
note (:func:`change_note`), never written into the prefix.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from critique_bot import agent_shell, integrations, log

READY_LINE = "Reply with exactly READY."
#: Memory files together, in bytes (UTF-8). Over it: a warning, and the least specific files are cut.
MEMORY_CAP = 32 * 1024
AGENTS_NAME = "AGENTS.md"
CRIT_NOTES = (".crit", "AGENT.md")
_NOTES_MARKER = "<!-- notes start -->"


def seed_message(
    workspace: Path,
    instructions: str,
    *,
    shell: agent_shell.Shell | None = None,
    notes: str = "",
    platform_name: str | None = None,
    persistent_cwd: bool = True,
    available: dict[str, agent_shell.Shell] | None = None,
    skills: list[str] | None = None,
    helpers: int = 0,
    web_hosts: tuple[str, ...] | None = None,
    offline: bool = False,
    role: str = "",
    tools: str = "",
) -> str:
    """The prefix: SHELL, the instructions, the ROLE text, the tools (text formats),
    ENVIRONMENT, and the memory files (``notes``). Nothing in it changes during a session."""
    chosen = shell or agent_shell.detect_shell(platform_name)
    lines = [
        "ENVIRONMENT",
        f"os: {platform_name or sys.platform}",
        f"shell: {chosen.label}",
        f"workspace: {workspace}",
    ]
    lines.extend(agent_shell.tool_hints(workspace, platform_name))
    if offline:
        lines.append(
            "network: offline. Commands reach no other machine: package managers run offline, the Gradle "
            "wrapper gets --offline, and git works with local repositories only. A build that needs a "
            "dependency that is not in the local cache fails; say which one in your report."
        )
    access = integrations.current()
    if access.gitlab_ready:
        lines.append(
            f"gitlab: {access.gitlab_url}. git clone, fetch, pull, ls-remote, and push work for the projects your "
            "GitLab token can reach (one plain git command each); other network commands follow the rules below."
        )
    if access.jira_ready:
        lines.append(f"jira: {access.jira_url} (read-only, with the jira tool)")
    sites = () if web_hosts is None else web_hosts
    if sites:
        lines.append("web_fetch sites (https, plain page addresses only): " + ", ".join(sites))
    else:
        lines.append("web_fetch: off (nothing leaves this machine but the chat)")
    if helpers > 0:
        lines.append(
            f"helper chats: {helpers}. Hand a self-contained part of the task to one with delegate (one brief, "
            "or up to 6 in one call). Each runs in a fresh chat and returns a short report, so the files it "
            "reads and the output it sees never land in this chat."
        )
    else:
        lines.append("helper chats: none (do not call delegate; do the work here)")
    if skills:
        lines.append(
            "skills: " + ", ".join(skills) + ". The ones that fit a task come with it under SKILLS; "
            "load another with the skill tool when the work turns to its domain."
        )
    if persistent_cwd:
        lines.append(
            "run_command starts in the workspace above. cd persists between run_command calls, "
            "as in a terminal; a result shows cwd when it changed. "
            "The result starts with exit, the code and seconds, then stdout, then stderr."
        )
    else:
        lines.append(
            "Each run_command is a new process whose starting folder is the workspace above, "
            "not C:\\ and not the user profile; pass cwd to start somewhere else. "
            "Its result starts with exit, the code and seconds, then cwd, then stdout, then stderr."
        )
    text = agent_shell.shell_preamble(chosen, available) + "\n\n"
    if instructions.strip():
        text += instructions.rstrip() + "\n\n"
    if role.strip():
        text += role.strip() + "\n\n"
    if tools.strip():
        text += tools.strip() + "\n\n"
    text += "\n".join(lines) + "\n"
    if notes.strip():
        if notes.lstrip().startswith("=== "):  # load_memory's files, each under its own heading
            heading = "PROJECT NOTES (memory files written by people; later files override earlier ones; follow them)"
        else:
            heading = "PROJECT NOTES (from .crit/AGENT.md; follow them)"
        text += "\n" + heading + "\n" + notes.strip() + "\n"
    if READY_LINE not in text:
        text += "\n" + READY_LINE + "\n"
    return text


def without_ready(seed: str) -> str:
    """The seed for a message that also carries the task: no READY handshake."""
    return re.sub(r"\n*[^\n]*\bexactly READY\b[^\n]*", "", seed).strip()


def prefix_hash(text: str) -> str:
    """A short hash of the prefix, logged with every request (it must not change in a session)."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# --------------------------------------------------------------------------- memory files


@dataclass
class MemoryFile:
    path: Path
    label: str
    text: str


@dataclass
class Memory:
    """The memory files that go into the prefix, in order (least specific first)."""

    files: list[MemoryFile] = field(default_factory=list)
    #: Set when the files were over the cap: what was cut. Shown to the person, not hidden.
    warning: str = ""

    @property
    def text(self) -> str:
        if not self.files:
            return ""
        parts = [f"=== {item.label} ===\n{item.text.strip()}" for item in self.files]
        if self.warning:
            parts.append(f"[{self.warning}]")
        return "\n\n".join(parts)


def _crit_notes(text: str) -> str:
    """``.crit/AGENT.md``: the text after the template's marker, or the whole file without one."""
    if _NOTES_MARKER in text:
        text = text.split(_NOTES_MARKER, 1)[1]
    return text.strip()


def _repo_root(workspace: Path) -> Path:
    """The nearest folder at or above ``workspace`` with ``.git``; ``workspace`` when there is none."""
    for folder in (workspace, *workspace.parents):
        if (folder / ".git").exists():
            return folder
    return workspace


def _read(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def memory_paths(workspace: Path, *, cwd: Path | None = None, user_dir: Path | None = None) -> list[tuple[Path, str]]:
    """(path, label) of every memory file that may apply, least specific first.

    ``~/.crit/AGENT.md`` (yours, every project), then from the repository root down to
    ``cwd`` (the workspace by default), in each folder ``AGENTS.md`` and ``.crit/AGENT.md``.
    """
    workspace = Path(workspace).resolve()
    here = Path(cwd).resolve() if cwd is not None else workspace
    if user_dir is None:
        try:
            from critique_bot.config import crit_home

            user_dir = crit_home()
        except Exception:
            user_dir = None
    found: list[tuple[Path, str]] = []
    if user_dir is not None:
        found.append((Path(user_dir) / "AGENT.md", "~/.crit/AGENT.md (your notes for every project)"))
    root = _repo_root(workspace)
    chain: list[Path] = []
    try:
        relative = here.relative_to(root)
        chain = [root]
        for part in relative.parts:
            chain.append(chain[-1] / part)
    except ValueError:
        chain = [here]
    for folder in chain:
        name = "." if folder == root else folder.relative_to(root).as_posix()
        found.append((folder / AGENTS_NAME, f"{name}/{AGENTS_NAME}" if name != "." else AGENTS_NAME))
        found.append((folder.joinpath(*CRIT_NOTES), f"{name}/.crit/AGENT.md" if name != "." else ".crit/AGENT.md"))
    seen: set[str] = set()
    unique: list[tuple[Path, str]] = []
    for path, label in found:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append((path, label))
    return unique


def load_memory(
    workspace: Path,
    *,
    cwd: Path | None = None,
    user_dir: Path | None = None,
    cap: int = MEMORY_CAP,
) -> Memory:
    """The memory files for a session, deeper ones last. Over ``cap`` bytes together, the
    least specific files are cut first and :attr:`Memory.warning` says so (it is also logged).

    crit never writes these files: they are for people to keep short and specific.
    """
    files: list[MemoryFile] = []
    for path, label in memory_paths(workspace, cwd=cwd, user_dir=user_dir):
        raw = _read(path)
        text = _crit_notes(raw) if path.name == "AGENT.md" else raw.strip()
        if text:
            files.append(MemoryFile(path, label, text))
    total = sum(len(item.text.encode("utf-8")) for item in files)
    memory = Memory(files)
    if total <= cap:
        return memory
    # Fill from the most specific file up; what does not fit is cut, and the person is told.
    room = cap
    kept: list[MemoryFile] = []
    cut: list[str] = []
    for item in reversed(files):
        size = len(item.text.encode("utf-8"))
        if size <= room:
            kept.append(item)
            room -= size
        elif room > 512:
            head = item.text.encode("utf-8")[:room].decode("utf-8", "ignore")
            kept.append(MemoryFile(item.path, item.label, head + "\n[cut: over the memory cap]"))
            cut.append(f"{item.label} (cut to {room} bytes)")
            room = 0
        else:
            cut.append(f"{item.label} (left out)")
    kept.reverse()
    memory = Memory(
        kept,
        warning=(
            f"memory files are {total // 1024} KiB, over the {cap // 1024} KiB cap: "
            + "; ".join(cut)
            + ". Keep AGENTS.md and .crit/AGENT.md short"
        ),
    )
    log.warn(memory.warning)
    return memory


# --------------------------------------------------------------------------- the first turn and notes


def git_status_line(workspace: Path, *, timeout: float = 5.0) -> str:
    """``branch main, 3 changed files (a.py, b.py, c.py)``, or "" outside git or on failure."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--branch"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    lines = proc.stdout.splitlines()
    branch = ""
    if lines and lines[0].startswith("## "):
        branch = lines.pop(0)[3:].split("...", 1)[0].strip()
    changed = [line[3:].strip() for line in lines if len(line) > 3]
    text = f"branch {branch}" if branch else "git repository"
    if changed:
        shown = ", ".join(changed[:10]) + (" ..." if len(changed) > 10 else "")
        text += f", {len(changed)} changed file{'s' if len(changed) != 1 else ''} ({shown})"
    else:
        text += ", no uncommitted changes"
    return text


def session_context(workspace: Path, *, now: datetime | None = None, git: bool = True) -> str:
    """What changes between sessions, for the first task message (never the prefix)."""
    moment = (now or datetime.now(timezone.utc)).astimezone()
    lines = ["SESSION", f"date: {moment.strftime('%Y-%m-%d %H:%M %Z').strip()}"]
    status = git_status_line(workspace) if git else ""
    if status:
        lines.append(f"git: {status}")
    return "\n".join(lines)


def change_note(kind: str, text: str) -> str:
    """A short note appended to the conversation when something changes mid-session."""
    label = {"mode": "MODE CHANGED", "role": "ROLE CHANGED", "permission": "PERMISSIONS CHANGED",
             "shell": "SHELL CHANGED"}.get(kind, kind.upper())
    return f"{label}: {text.strip()}"
