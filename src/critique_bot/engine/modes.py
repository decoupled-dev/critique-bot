# SPDX-License-Identifier: Apache-2.0
"""Permission modes: ask, edits, auto, plan (plan = read-only, then a plan the user approves).

The mode a task runs in is the session's, unless the UI switched it since the session
started (/mode, Shift+Tab, the Engine's set_mode): that switch wins, and the model is told
with an appended note.
"""

from __future__ import annotations

import re
from typing import Any

from critique_bot import agent_shell, prompt_build
from critique_bot import tool_api as agent_tools
from critique_bot.engine import ui
from critique_bot.engine.dispatch import _read_only_tools
from critique_bot.engine.parse import ToolCall, _hide_tool_markup
from critique_bot.tool_api import canonical_tool


_MODES = frozenset({"ask", "edits", "auto", "plan"})


#: What a mode change note tells the model (prompt_build.change_note).
_MODE_NOTES = {
    "ask": "ask. Edits and commands wait for the user's approval.",
    "edits": "edits. File edits in the workspace run without asking; commands still ask.",
    "auto": "auto. Edits and commands run without asking; risky commands still ask.",
    "plan": "plan. Read only now, then reply with a plan; nothing changes until the user approves it.",
}


MAX_PLAN_REVISIONS = 5


def _current_mode(default: str, at_start: str | None = None) -> str:
    """``default`` (the session's mode), unless the UI switched to another one since ``at_start``."""
    live = ui._ui_approve_mode()
    if live in _MODES and live != at_start:
        return live
    return default if default in _MODES else "ask"


class Modes:
    """Mode state of a task run (mixed into _TaskRun)."""

    def _approve_mode(self) -> str:
        """The mode now. /permissions in the UI can flip it mid-session; that change wins."""
        live = ui._ui_approve_mode()
        if live in _MODES and live != self.ui_mode_at_start:
            return live
        return self.approve_mode

    def _planning(self) -> bool:
        return not self.plan_done and self._approve_mode() == "plan"

    def _plan_allows(self, call: ToolCall, perm: Any) -> bool:
        """In plan mode: reads, read-only commands, and fetches run; edits and builds wait for the plan."""
        kind = getattr(perm, "kind", "read") or "read"
        name = canonical_tool(call.tool)
        if kind == "read" or kind == "network":
            return True
        if name in _read_only_tools():
            return True  # a read outside the workspace still asks
        if name in {"run_command", "command_input"}:
            check = getattr(agent_tools, "read_only_command", None)
            command = str(call.arguments.get("command") or call.arguments.get("input") or "")
            return bool(callable(check) and check(command))
        return False

    def _plan_reply(self, reply: str, status: str | None) -> str | None:
        """Plan mode: a reply with no calls is the plan. The user approves it, asks for changes, or stops."""
        plan = _hide_tool_markup(reply).strip()
        if status in {"FAILED", "BLOCKED"} and len(plan) > 40:
            return self._finish(status, reply=reply)
        words = re.sub(r"\b(?:COMPLETED|FINISHED|DONE)\b\.?", "", plan).strip()
        if len(words) < 40:
            self.plan_asks += 1
            if self.plan_asks > 2:
                return self._end("FAILED", "plan mode: the model did not send a plan")
            self.hooks.ui("note", "Asking for the plan.")
            self._send_message("PLAN_NEEDED")
            return None
        self.plans += 1
        choice, feedback = ui._review_plan()
        if choice in {"auto", "edits", "ask"}:
            self.plan_done = True
            self.approve_mode = choice
            self._send_message("PLAN_APPROVED")
            return None
        if choice == "no" and feedback and self.plans <= MAX_PLAN_REVISIONS:
            self._send_message("PLAN_REVISE", feedback=feedback)
            return None
        self.hooks.ui("note", "Plan only; no files were changed. Switch modes with Shift+Tab or /mode to carry it out.")
        self.hooks.status("COMPLETED")
        return "COMPLETED"

    def _note_mode_change(self) -> None:
        """A mode switched mid-task (/mode, Shift+Tab) goes to the model as a short appended note."""
        mode = self._approve_mode()
        if mode == self.mode_seen:
            return
        old, self.mode_seen = self.mode_seen, mode
        self.chat.log.write("mode_changed", old=old, new=mode)
        self.payload = prompt_build.change_note("mode", _MODE_NOTES.get(mode, mode)) + "\n\n" + self.payload

    def _note_shell_change(self) -> None:
        """After /shell, use the new shell and tell the model in the next message."""
        take = getattr(ui.current(), "take_shell_change", None)
        chosen = take() if callable(take) else None
        if chosen is None:
            return
        self.ctx.shell = chosen
        available = getattr(self.ctx.session, "available", None)
        try:
            preamble = agent_shell.shell_preamble(chosen, available)
        except Exception:  # a stand-in shell without the usual fields
            preamble = f"SHELL: {getattr(chosen, 'label', chosen)}."
        label = getattr(chosen, "label", "") or str(chosen)
        self.payload = f"Shell changed to {label}: {preamble}\n\n" + self.payload
