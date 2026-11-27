"""What the running session hands to every chat it opens: the native tool list and the extensions.

A session sets these once (run_agent_loop). Each value is kept for the session's own
thread and process-wide: helper chats run on their own threads and read the
process-wide one, and a second session on another thread (the Engine) keeps its own.
"""

from __future__ import annotations

import threading
from typing import Any

from critique_bot import log

_global: dict[str, Any] = {"native_tools": None, "extensions": None}
_local = threading.local()


def _get(key: str) -> Any:
    values = getattr(_local, "values", None)
    if values is not None and key in values:
        return values[key]
    return _global[key]


def native_tools() -> list[dict[str, Any]] | None:
    """The tools every chat of the running task gets natively (main and helper chats), or None."""
    return _get("native_tools")


def extensions() -> Any:
    """The session's extensions (extensions.ExtensionSession) while run_agent_loop runs, for the hooks."""
    return _get("extensions")


def start(*, native: list[dict[str, Any]] | None, extensions_: Any) -> None:
    """Set both values for this thread and process-wide (at the start of a session)."""
    values = {"native_tools": native, "extensions": extensions_}
    _local.values = values
    _global.update(values)


def finish() -> None:
    """The session is over: its values go (the process-wide ones only if they are still its own)."""
    values = getattr(_local, "values", None) or {}
    for key, value in values.items():
        if _global.get(key) is value and key == "extensions":
            _global[key] = None
    if values:
        values["extensions"] = None


def hook(event: str, payload: dict[str, Any]) -> Any:
    """Run the session's hooks for ``event``; None when there are none (see extensions/hooks.py)."""
    ext = extensions()
    if ext is None:
        return None
    try:
        return ext.run_hook(event, payload)
    except Exception as exc:  # a hook problem is reported, never a crash
        log.warn(f"{event} hook failed: {exc}")
        return None
