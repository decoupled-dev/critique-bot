# SPDX-License-Identifier: Apache-2.0
"""Hooks: programs crit runs at fixed points of a session, with a say in what happens.

``hooks.toml`` (or ``hooks.json``) in ``~/.crit`` or, once approved with ``crit trust``,
in the project's ``.crit``:

```toml
[[pre_tool]]
matcher = "run_command"            # optional: a regular expression on the tool name
argv = ["python3", "/home/me/.crit/hooks/no_force_push.py"]
timeout_s = 10                     # default 10

[[post_tool]]
matcher = "edit_file|write_files"
argv = ["ruff", "format", "--quiet", "."]
```

Events: ``session_start``, ``user_prompt``, ``pre_tool``, ``post_tool``, ``pre_compact``,
``task_finished``. Each hook gets the event as JSON on stdin and may print one JSON object:

```json
{"decision": "allow" | "deny" | "ask", "message": "why", "append_context": "text for the model"}
```

No output (exit 0) means allow. ``deny`` stops a tool call (pre_tool) or the task
(user_prompt). ``ask`` makes the call ask the user even in auto mode. A hook that times
out, cannot start, or prints something that is not JSON counts as ``ask``: a broken
policy hook must not wave calls through. Several hooks for one event: deny beats ask
beats allow, and their ``append_context`` texts are joined.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from critique_bot import log
from critique_bot.extensions import runner

__all__ = ["EVENTS", "Decision", "Hook", "HookError", "Hooks", "load_hooks"]

EVENTS = ("session_start", "user_prompt", "pre_tool", "post_tool", "pre_compact", "task_finished")
DEFAULT_TIMEOUT_S = 10.0
MAX_EVENT_CHARS = 20_000
_RANK = {"allow": 0, "ask": 1, "deny": 2}


class HookError(ValueError):
    """A hooks file is not valid."""


@dataclass(frozen=True)
class Hook:
    event: str
    argv: tuple[str, ...]
    matcher: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    source: str = ""

    def matches(self, tool: str) -> bool:
        if not self.matcher or not tool:
            return True
        try:
            return re.fullmatch(self.matcher, tool) is not None
        except re.error:
            return False


@dataclass
class Decision:
    decision: str = "allow"
    message: str = ""
    append_context: str = ""
    #: The hooks that spoke, for the log and the UI.
    sources: list[str] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

    @property
    def asks(self) -> bool:
        return self.decision == "ask"

    def merge(self, other: "Decision") -> None:
        if _RANK.get(other.decision, 1) > _RANK.get(self.decision, 0):
            self.decision = other.decision
            self.message = other.message or self.message
        elif other.message and other.decision == self.decision and not self.message:
            self.message = other.message
        if other.append_context:
            self.append_context = (self.append_context + "\n" + other.append_context).strip()
        self.sources.extend(other.sources)


def _read(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HookError(f"{path.name}: not valid JSON: {exc}") from exc
    else:
        if sys.version_info < (3, 11):
            raise HookError(f"{path.name}: TOML needs Python 3.11 or newer; use hooks.json")
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise HookError(f"{path.name}: not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise HookError(f"{path.name}: must be a table/object of events")
    return data


def load_hooks(path: Path) -> list[Hook]:
    data = _read(Path(path))
    hooks: list[Hook] = []
    for event, entries in data.items():
        if event not in EVENTS:
            raise HookError(f"{Path(path).name}: unknown event {event!r} (events: {', '.join(EVENTS)})")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            raise HookError(f"{Path(path).name}: {event} must be a list of hooks")
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise HookError(f"{Path(path).name}: {event}[{index}] must be a table/object")
            argv = entry.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                raise HookError(f"{Path(path).name}: {event}[{index}].argv must be a list of strings (no shell)")
            matcher = entry.get("matcher") or ""
            if not isinstance(matcher, str):
                raise HookError(f"{Path(path).name}: {event}[{index}].matcher must be a string")
            try:
                re.compile(matcher)
            except re.error as exc:
                raise HookError(f"{Path(path).name}: {event}[{index}].matcher: {exc}") from exc
            timeout = entry.get("timeout_s", DEFAULT_TIMEOUT_S)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 600:
                raise HookError(f"{Path(path).name}: {event}[{index}].timeout_s must be 1-600 seconds")
            hooks.append(Hook(event, tuple(argv), matcher, float(timeout), str(path)))
    return hooks


def _parse_output(stdout: str) -> Decision | None:
    text = stdout.strip()
    if not text:
        return Decision("allow")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # The last line may be the JSON after some chatter.
        try:
            data = json.loads(text.splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "allow").lower()
    if decision not in _RANK:
        return None
    return Decision(decision, str(data.get("message") or "")[:2000], str(data.get("append_context") or "")[:8000])


class Hooks:
    def __init__(self, hooks: list[Hook] | None = None, workspace: Path | None = None) -> None:
        self.hooks = list(hooks or [])
        self.workspace = Path(workspace) if workspace else Path.cwd()

    def __bool__(self) -> bool:
        return bool(self.hooks)

    def for_event(self, event: str, tool: str = "") -> list[Hook]:
        return [hook for hook in self.hooks if hook.event == event and hook.matches(tool)]

    def run(self, event: str, payload: dict[str, Any], *, tool: str = "", cancel: Any = None) -> Decision:
        """Run every hook for ``event`` (and ``tool``) and combine their decisions."""
        combined = Decision("allow")
        chosen = self.for_event(event, tool)
        if not chosen:
            return combined
        body = json.dumps({"event": event, "workspace": str(self.workspace), **payload}, ensure_ascii=False,
                          default=str)
        if len(body) > MAX_EVENT_CHARS * 4:
            body = json.dumps({"event": event, "workspace": str(self.workspace), "truncated": True,
                               **{k: v for k, v in payload.items() if k in {"tool", "prompt", "status"}}},
                              ensure_ascii=False, default=str)
        for hook in chosen:
            result = runner.run_argv(list(hook.argv), cwd=self.workspace, stdin=body.encode("utf-8"),
                                     timeout=hook.timeout_s, cancel=cancel)
            name = Path(hook.argv[0]).name
            if result.error:
                decision = Decision("ask", f"hook {name} could not run: {result.error}")
            elif result.timed_out:
                decision = Decision("ask", f"hook {name} timed out after {hook.timeout_s:g}s")
            else:
                parsed = _parse_output(result.stdout)
                if parsed is None:
                    decision = Decision("ask", f"hook {name} printed something that is not a decision")
                else:
                    decision = parsed
            decision.sources.append(f"{hook.source}:{event}:{name}")
            if decision.decision != "allow":
                log.info(f"hook {name} ({event}{' ' + tool if tool else ''}): {decision.decision} {decision.message}")
            combined.merge(decision)
        return combined
