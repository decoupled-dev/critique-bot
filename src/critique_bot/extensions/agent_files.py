# SPDX-License-Identifier: Apache-2.0
"""File-defined agents: a markdown file with frontmatter is an agent profile.

```markdown
---
name: reviewer
description: Reviews a diff for defects; read only.
mode: read-only                 # ask | edits | auto | plan | read-only
tools: [read_files, search_code, git_diff, git_log, git_show]
deny_tools: [web_fetch]
skills: [aosp]
max_steps: 30
max_depth: 0                    # may it delegate (helpers never do today)
max_parallel: 2                 # how many copies one delegate call may start
isolation: none                 # none | worktree (forced for agents that write)
output: summary                 # summary | review | json:<schema-file>
budget: {tokens: 200000, minutes: 20}
---
You are a careful reviewer ... (the agent's instructions)
```

Where they come from, later ones winning on a name clash: crit's built-in agents
(``critique_bot/agents/*.md``, one per former role), the active packs' agents
(``critique_bot/packs/<pack>/agents/*.md``), ``~/.crit/agents/*.md``, then the
project's ``.crit/agents/*.md``. A project agent is a prompt, so it loads without
approval, but until ``crit trust`` approves it its ``mode`` cannot be looser than ``ask``.

``model``, ``budget``, ``sandbox`` and ``flow`` are read and kept for the engine (Stage 5)
and sandbox (Stage 4); today a delegated agent runs on the session's model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from critique_bot.extensions import frontmatter, trust

__all__ = ["AgentDef", "AgentError", "builtin_dir", "find_agent", "load_agent", "load_agents", "MODES"]

MODES = ("ask", "edits", "auto", "plan", "read-only")
#: Modes a project agent may use before the user approves its file.
SAFE_MODES = frozenset({"ask", "plan", "read-only"})
ISOLATIONS = ("none", "worktree")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Tools that change files or run arbitrary commands: an agent with any of them writes.
WRITING_TOOLS = frozenset({"write_files", "edit_file", "delete_file", "move_file", "apply_patch", "run_command",
                           "command_input"})
_KNOWN = {"name", "description", "model", "mode", "tools", "deny_tools", "skills", "max_steps", "max_depth",
          "max_parallel", "isolation", "output", "budget", "sandbox", "flow", "role"}


class AgentError(ValueError):
    """An agent file is not valid."""


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    body: str
    mode: str = ""
    tools: tuple[str, ...] | None = None
    deny_tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    max_steps: int | None = None
    max_depth: int = 0
    max_parallel: int = 3
    isolation: str = "none"
    output: str = "summary"
    model: str = ""
    budget: dict[str, Any] = field(default_factory=dict)
    sandbox: str = ""
    flow: str = ""
    #: The role this built-in agent stands for (roles.py), "" for others.
    role: str = ""
    scope: str = "builtin"
    path: Path | None = None
    #: A project file the user approved (or a user/built-in one): its mode counts as written.
    trusted: bool = True

    def allowed_tools(self, available: Iterable[str]) -> list[str]:
        """The tools this agent may call, out of ``available``."""
        names = list(available)
        if self.tools is not None:
            wanted = set(self.tools)
            names = [name for name in names if name in wanted]
        if self.read_only:
            from critique_bot.tool_api import registry

            reg = registry()
            names = [name for name in names if reg.is_read_only(name) and name not in WRITING_TOOLS]
        denied = set(self.deny_tools)
        return [name for name in names if name not in denied]

    @property
    def read_only(self) -> bool:
        return self.mode in {"read-only", "plan"}

    @property
    def writes(self) -> bool:
        """True when the agent may change files or run commands (so it works in a worktree)."""
        if self.read_only:
            return False
        if self.tools is None:
            return True
        return bool((set(self.tools) - set(self.deny_tools)) & WRITING_TOOLS)

    @property
    def effective_mode(self) -> str:
        if self.mode and not self.trusted and self.mode not in SAFE_MODES:
            return "ask"
        return self.mode

    @property
    def effective_isolation(self) -> str:
        return "worktree" if self.writes else self.isolation

    def summary_line(self) -> str:
        return f"{self.name}: {self.description}"[:200]


def builtin_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "agents"


def _str_list(value: Any, key: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentError(f"{where}: {key} must be a list of names")
    return tuple(value)


def _int(value: Any, key: str, where: str, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise AgentError(f"{where}: {key} must be a whole number from {minimum} to {maximum}")
    return value


def parse_agent(text: str, *, path: Path | None = None, scope: str = "user", trusted: bool = True) -> AgentDef:
    where = path.name if path is not None else "agent"
    try:
        data, body = frontmatter.parse(text)
    except frontmatter.FrontmatterError as exc:
        raise AgentError(f"{where}: {exc}") from exc
    unknown = sorted(set(data) - _KNOWN)
    if unknown:
        raise AgentError(f"{where}: unknown keys {', '.join(unknown)}")
    name = data.get("name") or (path.stem if path is not None else "")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise AgentError(f"{where}: name must be lower-case letters, digits, - and _")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise AgentError(f"{where}: description is required (one line: what it is for)")
    mode = str(data.get("mode") or "")
    if mode and mode not in MODES:
        raise AgentError(f"{where}: mode must be one of {', '.join(MODES)}")
    isolation = str(data.get("isolation") or "none")
    if isolation not in ISOLATIONS:
        raise AgentError(f"{where}: isolation must be none or worktree")
    output = str(data.get("output") or "summary")
    if output not in {"summary", "review"} and not output.startswith("json:"):
        raise AgentError(f"{where}: output must be summary, review, or json:<schema-file>")
    budget = data.get("budget") or {}
    if not isinstance(budget, dict):
        raise AgentError(f"{where}: budget must be a map like {{tokens: 400000, minutes: 45}}")
    tools = data.get("tools")
    return AgentDef(
        name=name,
        description=description.strip(),
        body=body.strip(),
        mode=mode,
        tools=None if tools is None else _str_list(tools, "tools", where),
        deny_tools=_str_list(data.get("deny_tools"), "deny_tools", where),
        skills=_str_list(data.get("skills"), "skills", where),
        max_steps=_int(data.get("max_steps"), "max_steps", where, 1, 500),
        max_depth=_int(data.get("max_depth"), "max_depth", where, 0, 3) or 0,
        max_parallel=_int(data.get("max_parallel"), "max_parallel", where, 1, 6) or 3,
        isolation=isolation,
        output=output,
        model=str(data.get("model") or ""),
        budget=dict(budget),
        sandbox=str(data.get("sandbox") or ""),
        flow=str(data.get("flow") or ""),
        role=str(data.get("role") or ""),
        scope=scope,
        path=path,
        trusted=trusted,
    )


def load_agent(path: Path, *, scope: str = "user") -> AgentDef:
    path = Path(path)
    trusted = scope != "project" or trust.is_trusted(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentError(f"{path.name}: {exc}") from exc
    return parse_agent(text, path=path, scope=scope, trusted=trusted)


def load_agents(root: Path | None) -> tuple[dict[str, AgentDef], list[str]]:
    """Every agent by name (built-in, active packs, then user, then project) and the problems found."""
    agents: dict[str, AgentDef] = {}
    problems: list[str] = []
    files = [trust.ExtFile("agents", "builtin", path) for path in sorted(builtin_dir().glob("*.md"))]
    from critique_bot import packs

    for folder in packs.agent_dirs():  # the active domain packs' agents (built-in content)
        files += [trust.ExtFile("agents", "pack", path) for path in sorted(folder.glob("*.md"))]
    files += trust.discover(root, ["agents"])
    for item in files:
        try:
            agent = load_agent(item.path, scope=item.scope)
        except AgentError as exc:
            problems.append(str(exc))
            continue
        agents[agent.name] = agent
    return agents, problems


def find_agent(name: str, root: Path | None) -> AgentDef | None:
    """The agent called ``name`` (a role id or alias works for the built-ins)."""
    agents, _ = load_agents(root)
    key = str(name or "").strip().lower()
    if key in agents:
        return agents[key]
    from critique_bot import roles

    role = roles.normalize_role(key)
    if role:
        for agent in agents.values():
            if agent.role == role or agent.name == role:
                return agent
    return None
