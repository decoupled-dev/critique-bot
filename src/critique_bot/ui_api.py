"""The engine's UI interface: how the agent loop shows progress and asks a person.

The loop never imports a front end. It calls the object installed with
``agent.use_ui(...)``: the terminal (:mod:`critique_bot.agent_ui`) or an IDE over the
Agent Client Protocol (:mod:`critique_bot.agent_acp`). Each is a module whose functions match
:class:`NullUI`'s methods. :class:`NullUI` is the default: it shows nothing and answers
every question the way the terminal does when no one is at it, so the engine runs (and
is tested) headless with no front end at all.

Permission modes live here too: they are engine state that every front end shows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The task text that asks for a fresh chat.
NEW_CHAT = "/new"
#: Shift+Tab cycles through these, in this order.
MODES = ("ask", "edits", "auto", "plan")
MODE_LABELS = {
    "ask": "ask before edits and commands",
    "edits": "accept edits on (commands still ask)",
    "auto": "auto mode on (asks only for risky commands)",
    "plan": "plan mode on (reads only, then a plan to approve)",
}
_MODE_ALIASES = {
    "default": "ask", "ask": "ask", "normal": "ask", "manual": "ask",
    "edits": "edits", "edit": "edits", "accept": "edits", "accept-edits": "edits", "acceptedits": "edits",
    "accept_edits": "edits",
    "auto": "auto", "yes": "auto", "yolo": "auto", "bypass": "auto", "bypasspermissions": "auto", "allow": "auto",
    "plan": "plan", "planning": "plan",
}


def normalize_mode(raw: Any, default: str = "ask") -> str:
    """``ask``, ``edits``, ``auto``, or ``plan`` from a setting, flag, or command word."""
    key = str(raw or "").strip().lower().replace(" ", "-")
    return _MODE_ALIASES.get(key, default)


class NullUI:
    """A UI with no person: shows nothing, approves nothing, asks nothing.

    Approvals answer no and questions get no answer, as the terminal does without a TTY,
    so nothing runs that would have needed a yes. Front ends implement the same names.
    """

    NEW_CHAT = NEW_CHAT

    def __init__(self) -> None:
        self._mode = "ask"

    # session
    def configure(self, **settings: Any) -> None:
        mode = settings.get("approve_mode")
        if mode:
            self._mode = normalize_mode(mode, self._mode)

    def welcome_header(self) -> None:
        return None

    def install_log_bridge(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def normalize_mode(self, raw: Any, default: str = "ask") -> str:
        return normalize_mode(raw, default)

    def approve_mode(self) -> str:
        return self._mode

    # output
    def note(self, kind: str, message: str) -> None:
        return None

    def final_status(self, code: str, *, ok: bool) -> None:
        return None

    def assistant(self, text: str) -> None:
        return None

    def tool_start(self, name: str, arguments: dict[str, Any]) -> None:
        return None

    def tool_done(self, name: str, arguments: dict[str, Any], result: dict[str, Any], *, friendly: str = "") -> None:
        return None

    def tool_output(self, text: str) -> None:
        return None

    def waiting(self, active: bool, label: str = "") -> None:
        return None

    def set_active(self, kind: str, count: int) -> None:
        return None

    # questions
    def read_message(self) -> str | None:
        return None

    def approve(self, permission: Any, *, risky: str = "") -> tuple[str, str]:
        return ("no", "no one to approve" + (f" a command that {risky}" if risky else ""))

    def review_plan(self) -> tuple[str, str]:
        return ("none", "")

    def ask_question(self, question: str, options: list[str] | None = None) -> str | None:
        return None

    # changes made from the UI since the last call (none without a UI)
    def take_shell_change(self) -> Any:
        return None

    def take_role_change(self) -> str | None:
        return None

    def pinned_skills(self) -> list[str]:
        return []

    def handle_command(self, word: str, rest: str = "") -> None:
        return None

    def workspace(self) -> Path | None:
        return None
