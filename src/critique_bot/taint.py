# SPDX-License-Identifier: Apache-2.0
"""Taint: untrusted content entered the session, so the agent's reach shrinks.

The lethal trifecta is private data + untrusted content + a way to send data out. crit
keeps the first (it works on your code); this module removes the third once the second
happens, instead of trusting the model to ignore injected instructions.

Sources (each marks the session tainted, with an audit record and a notice):

* ``web_fetch`` results;
* files read from outside the workspace;
* issue text from Jira (and GitLab, MCP tool output later);
* device logs (the ``log`` tool on a device log, ``adb logcat``);
* a workspace marked untrusted (``"untrusted_workspace": true`` in settings: a freshly
  cloned third-party repo), which taints from the start.

While the session is tainted **and in auto mode**, :func:`check` denies: fetches and
other network calls (``web_fetch``, network commands, ``git push``), writes and commands
outside the workspace; the egress proxy lets sandboxed commands reach only the model
server. Other modes keep asking as usual (a person sees each such call). ``/trust`` lifts
it (:func:`trust`, logged).

The engine calls :func:`check` around its permission decision (one call in
``agent.py``); the tools call :func:`observe` after each tool result (``agent_tools.execute``).
Tools layer: it needs ``agent_tools.network_command``.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from critique_bot import audit, log


@dataclass
class State:
    tainted: bool = False
    sources: list[str] = field(default_factory=list)
    #: The approval mode last seen by check(): restrictions apply in "auto" only.
    mode: str = ""
    lifted: int = 0


_state = State()
_lock = threading.Lock()
_listeners: list[Callable[[str], None]] = []


def state() -> State:
    return _state


def reset() -> None:
    """Back to untainted (a new session; tests)."""
    global _state
    with _lock:
        _state = State()


def start_session(*, untrusted: bool = False, workspace: Path | None = None) -> None:
    """A new agent session: untainted, unless the workspace itself is marked untrusted
    (``"untrusted_workspace": true`` in settings, for a freshly cloned third-party repo)."""
    reset()
    if untrusted:
        mark("untrusted_workspace", str(workspace or ""))


def on_change(listener: Callable[[str], None]) -> None:
    """Call ``listener(text)`` when the session becomes tainted or is trusted again (the UI's notice)."""
    if listener not in _listeners:
        _listeners.append(listener)


def _notify(text: str) -> None:
    for listener in list(_listeners):
        try:
            listener(text)
        except Exception:  # noqa: BLE001 - a UI problem never stops a tool
            pass


def mark(source: str, detail: str = "") -> bool:
    """Taint the session because of ``source`` (``web_fetch``, ``outside_read``, ...). True when it was new."""
    label = f"{source}: {detail}" if detail else source
    with _lock:
        first = not _state.tainted
        _state.tainted = True
        if label not in _state.sources:
            _state.sources.append(label)
            del _state.sources[:-20]
        else:
            return False
    audit.record("taint", event="raised", source=source, detail=detail)
    log.info(f"session tainted by {label}")
    if first:
        _notify(f"Untrusted content entered the session ({label}). In auto mode, network access, pushes, and "
                "writes outside the workspace are now refused. /trust lifts this.")
    return first


def trust(reason: str = "/trust") -> bool:
    """Lift the taint (the user said so). True when the session was tainted."""
    with _lock:
        was = _state.tainted
        sources = list(_state.sources)
        _state.tainted = False
        _state.sources = []
        _state.lifted += 1
    if was:
        audit.record("taint", event="lifted", reason=reason, sources=sources)
        log.info(f"taint lifted ({reason})")
        _notify("Trusted again: the taint restrictions are lifted.")
    return was


def tainted() -> bool:
    return _state.tainted


def restricted() -> bool:
    """Tainted and in auto mode: the restrictions apply."""
    return _state.tainted and _state.mode == "auto"


def marker() -> str:
    """The status-line marker ("" when untainted)."""
    if not _state.tainted:
        return ""
    return "tainted" + (" · restricted" if _state.mode == "auto" else "")


def describe() -> str:
    if not _state.tainted:
        return "not tainted"
    return "tainted by " + "; ".join(_state.sources[-5:])


# --------------------------------------------------------------------------- what is untrusted

_LOGCAT_RE = re.compile(r"(?:^|[\s;&|(])adb(?:\s+-[sdeHPL]\s*\S+|\s+-[de])*\s+(?:logcat|bugreport|shell\s+(?:logcat|dumpsys|dmesg|cat\s+/))")


def observe(tool: str, args: dict[str, Any] | None, result: dict[str, Any] | None, workspace: Path | None = None) -> str:
    """Mark the session when a tool result carries untrusted text; the source, or ""."""
    if not isinstance(result, dict) or not result.get("ok"):
        return ""
    args = args if isinstance(args, dict) else {}
    source = detail = ""
    if tool == "web_fetch":
        source, detail = "web_fetch", str(args.get("url") or "")[:200]
    elif tool == "jira":
        source, detail = "jira", str(args.get("key") or args.get("issue") or args.get("query") or "")[:120]
    elif tool in {"gitlab", "gitlab_issue", "gitlab_mr"}:
        source, detail = "gitlab", ""
    elif tool.startswith("mcp__") or tool.startswith("mcp."):
        source, detail = "mcp", tool
    elif tool == "log" and _device_log(args):
        source, detail = "device_log", str(args.get("path") or args.get("source") or "")[:200]
    elif tool == "run_command" and _LOGCAT_RE.search(str(args.get("command") or "")):
        source, detail = "device_log", str(args.get("command") or "")[:120]
    elif tool in {"read_files", "search_code", "list_files", "find_files"} and workspace is not None:
        outside = _outside_paths(args, workspace)
        if outside:
            source, detail = "outside_read", outside[0]
    if source:
        mark(source, detail)
    return source


def _device_log(args: dict[str, Any]) -> bool:
    text = " ".join(str(args.get(key) or "") for key in ("path", "source", "device", "command")).lower()
    return any(word in text for word in ("logcat", "adb", "device", "bugreport", "dmesg", "tombstone", "anr"))


def _outside_paths(args: dict[str, Any], workspace: Path) -> list[str]:
    raw: list[str] = []
    for key in ("path", "paths", "file", "files", "dir"):
        value = args.get(key)
        if isinstance(value, str):
            raw.append(value)
        elif isinstance(value, list):
            raw += [str(item.get("path") if isinstance(item, dict) else item) for item in value]
    root = Path(workspace).resolve()
    out = []
    for item in raw:
        text = item.strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            out.append(text)
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------- the engine hook


def restriction(perm: Any) -> str:
    """Why ``perm`` is refused under taint in auto mode ("" when it is not)."""
    if not restricted():
        return ""
    kind = str(getattr(perm, "kind", "") or "")
    tool = str(getattr(perm, "tool", "") or "")
    detail = str(getattr(perm, "detail", "") or "")
    why = ""
    if kind == "network" or tool == "web_fetch":
        why = "it reaches the network"
    elif kind in {"command", "outside"} and detail and _network(detail):
        why = "it reaches the network"
    elif kind == "outside" and tool not in _READ_TOOLS:
        why = "it changes or runs something outside the workspace"
    if not why:
        return ""
    return (f"the session is tainted ({'; '.join(_state.sources[-2:])}) and in auto mode, so this is refused: {why}. "
            "Untrusted text may be steering the model. The user can lift this with /trust")


_READ_TOOLS = frozenset({"read_files", "search_code", "list_files", "find_files", "git_show", "git_log", "git_diff",
                         "git_status", "log"})


def _network(command: str) -> bool:
    try:
        from critique_bot.agent_tools import network_command

        return bool(network_command(command))
    except Exception:  # noqa: BLE001
        return False


def check(perm: Any, mode: str, approve: Callable[[Any], tuple[bool, str]]) -> tuple[bool, str]:
    """The engine's permission decision with taint applied, recorded in the audit log.

    ``approve`` is the engine's own decision (it may ask the user). Under taint in auto mode a
    restricted call is refused without asking.
    """
    _state.mode = str(mode or "")
    kind = str(getattr(perm, "kind", "read") or "read")
    refused = restriction(perm)
    if refused:
        allowed, reason = False, refused
    else:
        allowed, reason = approve(perm)
    if kind != "read":
        audit.record(
            "permission", tool=str(getattr(perm, "tool", "") or ""), perm_kind=kind, mode=_state.mode,
            summary=str(getattr(perm, "summary", "") or ""), allowed=bool(allowed), reason=reason,
            tainted=_state.tainted, contained=bool(getattr(perm, "contained", False)),
        )
    return allowed, reason
