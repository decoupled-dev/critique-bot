"""The engine: the agent loop behind every crit front end, and its public API.

Apps use only the names below (``tests/test_layers.py`` keeps the other engine modules
private to the engine and the routing layer):

- ``run_agent`` / ``run_read_only_task``: one whole session (open the backend, set the
  session up, run the loop, save the transcript); ``AnswerContract`` for a read-only
  task whose final answer an app parses.
- ``run_agent_loop``: the loop on an open model session (tests, helper code).
- ``use_ui`` / ``current_ui``: the process-wide front end (terminal or ACP).
- ``ToolCall``, ``activity``, ``hide_tool_markup``, ``READ_ONLY_TOOLS``: small pieces front
  ends show or check.

The package: ``loop`` (sessions), ``task`` (the turn loop), ``dispatch`` (tool calls and
permissions), ``modes``, ``stop_rules``, ``contracts``, ``delegate`` (helper chats and file
agents), ``chat`` (the conversation and its history), ``context`` (session log and
compaction hooks), ``prompts``, ``parse`` / ``json_repair`` / ``replies`` (reading replies),
``ui`` and ``runtime`` (what a running session is connected to), ``setup`` and ``runs``
(a whole run). ``session_log``, ``compaction`` and ``prompt_build`` stay sibling engine
modules (Stage 3), used here and by ``crit sessions``.
"""

from __future__ import annotations

from critique_bot.engine.contracts import AnswerContract, run_read_only_task
from critique_bot.engine.dispatch import READ_ONLY_TOOLS
from critique_bot.engine.loop import run_agent_loop
from critique_bot.engine.parse import ToolCall, hide_tool_markup
from critique_bot.engine.runs import run_agent
from critique_bot.engine.ui import _activity as activity
from critique_bot.engine.ui import current as current_ui
from critique_bot.engine.ui import use_ui

__all__ = [
    "AnswerContract",
    "READ_ONLY_TOOLS",
    "ToolCall",
    "activity",
    "current_ui",
    "hide_tool_markup",
    "run_agent",
    "run_agent_loop",
    "run_read_only_task",
    "use_ui",
]
