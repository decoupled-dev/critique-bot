"""Saved permission rules: ``"allow"`` and ``"deny"`` in ``.bot/settings.json``.

A rule is a command pattern (``"./gradlew test*"``, ``"git status*"``) or a tool with a
path pattern (``"read(~/.gradle/**)"``, ``"edit(docs/**)"``), like Claude Code's
``Bash(npm run test:*)``. ``*`` matches any text in a command; in a path, ``*`` stays
inside one folder and ``**`` crosses folders.

Deny rules apply in every mode and in helper chats. Allow rules skip the question for a
matching step, but never for a network command, a risky command, a secret file, or a
file the task just wrote: those keep asking (see ``Agent._approved``).
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

#: Names accepted before the parentheses, and the kind each one means.
_TOOL_KINDS = {
    "run_command": "command",
    "command": "command",
    "bash": "command",
    "shell": "command",
    "powershell": "command",
    "pwsh": "command",
    "cmd": "command",
    "read": "read",
    "read_files": "read",
    "edit": "edit",
    "write": "edit",
    "write_files": "edit",
}
_RULE_RE = re.compile(r"^\s*(?P<tool>[A-Za-z_]+)\s*\((?P<pattern>.*)\)\s*$", re.S)
_SPLIT_RE = re.compile(r"(&&|\|\||[;|&\r\n])")
_CD = {"cd", "set-location", "sl", "pushd", "popd", "chdir"}
_FILTERS = {"head", "tail", "grep", "egrep", "fgrep", "rg", "sort", "uniq", "wc", "cut", "tr", "findstr",
            "select-string", "sls", "select-object", "select", "out-string", "out-null", "format-table", "ft",
            "format-list", "fl", "measure-object", "more", "less", "column", "true"}
_HARMLESS_REDIRECT = re.compile(r"(?<![^\s])(?:[12*]?>&[12]|[12*]?>\s*(?:/dev/null|nul|\$null))(?=\s|$)", re.I)


@dataclass(frozen=True)
class Rule:
    kind: str  # command, read, or edit
    pattern: str
    text: str  # as written in settings

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class Rules:
    allow: tuple[Rule, ...] = ()
    deny: tuple[Rule, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.allow or self.deny)


def parse_rule(text: str) -> Rule | None:
    """``"./gradlew test*"`` -> a command rule; ``"read(~/.gradle/**)"`` -> a read rule; None if empty or unknown."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _RULE_RE.match(raw)
    if match is not None:
        kind = _TOOL_KINDS.get(match.group("tool").lower())
        if kind is None:
            return None
        pattern = match.group("pattern").strip()
        return Rule(kind, pattern, raw) if pattern else None
    return Rule("command", raw, raw)


def load_rules(settings: dict[str, Any] | None) -> tuple[Rules, list[str]]:
    """Rules from ``settings["allow"]`` and ``settings["deny"]``, and a problem line for each bad entry."""
    settings = settings if isinstance(settings, dict) else {}
    problems: list[str] = []
    out: dict[str, list[Rule]] = {"allow": [], "deny": []}
    for key in ("allow", "deny"):
        raw = settings.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            problems.append(f'"{key}" must be a list of rules, for example ["./gradlew test*", "read(~/.gradle/**)"]')
            continue
        for item in raw:
            rule = parse_rule(item) if isinstance(item, str) else None
            if rule is None:
                problems.append(f'"{key}": {item!r} is not a rule')
                continue
            out[key].append(rule)
    return Rules(tuple(out["allow"]), tuple(out["deny"])), problems


# --------------------------------------------------------------------------- commands


def _command_glob(pattern: str) -> re.Pattern[str]:
    text = " ".join(pattern.split())
    parts = [re.escape(piece) for piece in text.split("*")]
    return re.compile("^" + ".*".join(parts) + "$", re.I if sys.platform == "win32" else 0)


def _program(segment: str) -> str:
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        tokens = segment.split()
    for token in tokens:
        token = token.strip("\"'").lstrip("&").strip()
        if not token or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return re.split(r"[\\/]", token.lower())[-1].removesuffix(".exe")
    return ""


def command_segments(command: str) -> list[str] | None:
    """The parts of ``command`` a rule must cover, or None when no rule can cover it.

    ``cd app && ./gradlew test | tail -20`` -> ``["./gradlew test"]``: a ``cd`` and an
    output filter after a pipe need no rule. A subexpression (``$(...)``, backticks), a
    script block, or a redirection into a file makes the command uncheckable: None.
    """
    text = " ".join(str(command or "").split())
    if not text or "`" in text or "$(" in text:
        return None
    bare = _HARMLESS_REDIRECT.sub(" ", text)
    quote = ""
    for index, char in enumerate(bare):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            if index and bare[index - 1] == "@":
                return None
            quote = char
        elif char in "(){}<>":
            return None
    if quote:
        return None
    parts = _SPLIT_RE.split(bare)
    segments: list[str] = []
    separator = ""
    for index, part in enumerate(parts):
        if index % 2:
            separator = part
            continue
        segment = part.strip()
        program = _program(segment)
        if not program or program in _CD:
            continue
        if separator in {"|", "||"} and program in _FILTERS:
            continue
        segments.append(segment)
    return segments or None


def _command_matches(command: str, rule: Rule) -> bool:
    glob = _command_glob(rule.pattern)
    segments = command_segments(command)
    if segments is None:
        return False
    return all(glob.match(segment) for segment in segments)


def _command_denied(command: str, rule: Rule) -> bool:
    glob = _command_glob(rule.pattern)
    text = " ".join(str(command or "").split())
    if glob.match(text):
        return True
    # Every piece of a chain is checked, whatever joins it: "cd x && rm -rf y" hits "rm -rf*".
    for part in _SPLIT_RE.split(text)[::2]:
        part = part.strip().lstrip("({").strip()
        if part and glob.match(part):
            return True
    return False


def suggest_rule(command: str) -> str:
    """The allow rule a "don't ask again" answer saves: ``./gradlew test --info`` -> ``./gradlew test*``.

    The program and its first word that is not a flag or a path, so the rule covers that
    kind of command and not every use of the program. "" when no rule can cover it.
    """
    segments = command_segments(command)
    if not segments or len({_program(segment) for segment in segments}) != 1:
        return ""
    try:
        tokens = shlex.split(segments[0], posix=False)
    except ValueError:
        tokens = segments[0].split()
    words: list[str] = []
    for token in tokens:
        if not words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            return ""  # an environment assignment in front: too specific to generalize
        words.append(token)
        if len(words) == 1:
            continue
        if token.startswith(("-", "/", "\\", ".", "~", "$", "%")) or re.search(r"[\\/=:*?\"'<>|]", token):
            words.pop()
        break
    return " ".join(words) + "*" if words else ""


# --------------------------------------------------------------------------- paths


def _path_regex(pattern: str, workspace: Path) -> re.Pattern[str]:
    text = os.path.expanduser(pattern.strip())
    base = Path(text) if Path(text).is_absolute() else Path(workspace) / text
    norm = base.as_posix()
    # Keep a trailing "**" or "*" through normalization; resolve the folder part only.
    out = []
    index = 0
    while index < len(norm):
        if norm.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif norm.startswith("**", index):
            out.append(".*")
            index += 2
        elif norm[index] == "*":
            out.append("[^/]*")
            index += 1
        elif norm[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(norm[index]))
            index += 1
    flags = re.I if sys.platform == "win32" else 0
    return re.compile("^" + "".join(out) + "$", flags)


def _path_matches(path: str | Path, rule: Rule, workspace: Path) -> bool:
    target = Path(os.path.abspath(os.path.expanduser(str(path)))).as_posix()
    workspace = Path(os.path.abspath(workspace))
    return bool(_path_regex(rule.pattern, workspace).match(target))


# --------------------------------------------------------------------------- decisions


def _kind_of(tool: str, perm_kind: str) -> str:
    if tool in {"run_command", "command_input"}:
        return "command"
    if tool in {"write_files", "edit_file", "delete_file", "apply_patch", "move_file"}:
        return "edit"
    if perm_kind in {"edit"}:
        return "edit"
    return "read"


def _matches(rule: Rule, *, kind: str, command: str, paths: Iterable[str], workspace: Path, deny: bool) -> bool:
    if rule.kind != kind:
        return False
    if kind == "command":
        return _command_denied(command, rule) if deny else _command_matches(command, rule)
    paths = list(paths)
    if not paths:
        return False
    if deny:
        return any(_path_matches(path, rule, workspace) for path in paths)
    return all(_path_matches(path, rule, workspace) for path in paths)


def denied_by(rules: Rules, *, tool: str, perm_kind: str, command: str, paths: Iterable[str], workspace: Path) -> Rule | None:
    """The deny rule that covers this step, or None."""
    kind = _kind_of(tool, perm_kind)
    paths = list(paths)
    for rule in rules.deny:
        if _matches(rule, kind=kind, command=command, paths=paths, workspace=workspace, deny=True):
            return rule
    return None


def allowed_by(rules: Rules, *, tool: str, perm_kind: str, command: str, paths: Iterable[str], workspace: Path) -> Rule | None:
    """The allow rule that covers this step (every command part, or every path), or None."""
    kind = _kind_of(tool, perm_kind)
    if kind == "command":
        # Each part of a chain needs a rule, not necessarily the same one.
        segments = command_segments(command)
        if segments is None:
            return None
        globs = [(rule, _command_glob(rule.pattern)) for rule in rules.allow if rule.kind == "command"]
        used: Rule | None = None
        for segment in segments:
            hit = next((rule for rule, glob in globs if glob.match(segment)), None)
            if hit is None:
                return None
            used = used or hit
        return used
    paths = list(paths)
    for rule in rules.allow:
        if _matches(rule, kind=kind, command=command, paths=paths, workspace=workspace, deny=False):
            return rule
    return None


def with_allow(rules: Rules, text: str) -> Rules:
    rule = parse_rule(text)
    if rule is None or any(item.text == rule.text for item in rules.allow):
        return rules
    return Rules(rules.allow + (rule,), rules.deny)
