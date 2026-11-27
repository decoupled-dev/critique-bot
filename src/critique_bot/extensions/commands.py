"""Custom slash commands: ``.crit/commands/<name>.md`` (or ``~/.crit/commands``) becomes ``/<name>``.

```markdown
---
description: Fix a Jira issue end to end
agent: expert-developer          # optional: the agent (persona) for this task
mode: plan                       # optional: ask | edits | auto | plan (never looser than the session
                                 #   for a project command the user has not approved)
arguments: [issue]               # optional: names, for /help and the $issue placeholder
---
Fix $1. Read the issue with the jira tool first, reproduce it, then fix it and run the tests.
Everything typed after the command: $ARGUMENTS
```

``$ARGUMENTS`` is everything after the command, ``$1`` ... ``$9`` the words, ``$name`` the
word for a named argument. Commands only build a task's text: they run nothing themselves.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from critique_bot.extensions import frontmatter, trust

__all__ = ["Command", "CommandError", "expand", "load_commands"]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
#: Modes a project command may set before the user approves its file.
SAFE_MODES = frozenset({"ask", "plan"})


class CommandError(ValueError):
    """A command file is not valid."""


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    template: str
    agent: str = ""
    mode: str = ""
    arguments: tuple[str, ...] = ()
    scope: str = "user"
    path: Path | None = None
    trusted: bool = True

    @property
    def word(self) -> str:
        return "/" + self.name

    @property
    def effective_mode(self) -> str:
        if self.mode and not self.trusted and self.mode not in SAFE_MODES:
            return ""
        return self.mode

    def expand(self, rest: str) -> str:
        return expand(self.template, rest, self.arguments)


def expand(template: str, rest: str, names: tuple[str, ...] = ()) -> str:
    """The template with ``$ARGUMENTS``, ``$1``..``$9`` and ``$<name>`` filled in from ``rest``."""
    rest = rest.strip()
    try:
        words = shlex.split(rest)
    except ValueError:
        words = rest.split()
    values = {str(index): word for index, word in enumerate(words, start=1)}
    for index, name in enumerate(names, start=1):
        values[name] = words[index - 1] if index <= len(words) else ""

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "ARGUMENTS":
            return rest
        if key in values:
            return values[key]
        if key.isdigit():
            return ""
        return match.group(0)

    text = re.sub(r"\$(ARGUMENTS|\d|[A-Za-z_][A-Za-z0-9_]*)", fill, template)
    if "$ARGUMENTS" not in template and not re.search(r"\$\d", template) and rest and not names:
        text = text.rstrip() + "\n\n" + rest
    return text.strip()


def parse_command(text: str, *, path: Path, scope: str = "user", trusted: bool = True) -> Command:
    try:
        data, body = frontmatter.parse(text)
    except frontmatter.FrontmatterError as exc:
        raise CommandError(f"{path.name}: {exc}") from exc
    name = path.stem.lower()
    if not _NAME_RE.match(name):
        raise CommandError(f"{path.name}: the file name must be lower-case letters, digits, - and _")
    if not body.strip():
        raise CommandError(f"{path.name}: the body (the task template) is empty")
    arguments = data.get("arguments") or []
    if isinstance(arguments, str):
        arguments = arguments.split()
    names = []
    for item in arguments if isinstance(arguments, list) else []:
        if isinstance(item, dict):
            item = item.get("name")
        if isinstance(item, str) and re.match(r"^[A-Za-z_]\w*$", item):
            names.append(item)
    mode = str(data.get("mode") or "")
    if mode and mode not in {"ask", "edits", "auto", "plan"}:
        raise CommandError(f"{path.name}: mode must be ask, edits, auto, or plan")
    return Command(
        name=name,
        description=str(data.get("description") or body.strip().splitlines()[0])[:160],
        template=body.strip(),
        agent=str(data.get("agent") or ""),
        mode=mode,
        arguments=tuple(names),
        scope=scope,
        path=path,
        trusted=trusted,
    )


def load_commands(root: Path | None, *, reserved: frozenset[str] | set[str] = frozenset()) -> tuple[dict[str, Command], list[str]]:
    """Commands by their word (``/fix-issue``), user then project, and the problems found.

    A command named like a built-in slash command (``reserved``) is skipped: built-ins win.
    """
    found: dict[str, Command] = {}
    problems: list[str] = []
    for item in trust.discover(root, ["commands"]):
        trusted_file = item.scope != "project" or trust.is_trusted(item.path)
        try:
            command = parse_command(item.path.read_text(encoding="utf-8"), path=item.path, scope=item.scope,
                                    trusted=trusted_file)
        except (OSError, CommandError) as exc:
            problems.append(str(exc))
            continue
        if command.word in reserved:
            problems.append(f"{item.path.name}: /{command.name} is a built-in command; rename the file")
            continue
        found[command.word] = command
    return found, problems
