# SPDX-License-Identifier: Apache-2.0
"""Compatibility names for code written against the old ``critique_bot.agent`` module.

The agent loop now lives in :mod:`critique_bot.engine` (Stage 5): apps use the engine's
interface (``Engine``, ``SessionHandle``, ``run_agent``, ``run_read_only_task``, ...).
This module re-exports what older callers and tests import from here, and reads
``agent.agent_ui`` (the UI installed now) through a module ``__getattr__``. New code
imports from ``critique_bot.engine``; this module goes once nothing uses it.
"""

from __future__ import annotations

import sys  # noqa: F401  (tests patch agent.sys.platform)
from pathlib import Path  # noqa: F401  (tests patch agent.Path.home)
from typing import Any

from critique_bot import agent_shell  # noqa: F401
from critique_bot.engine import ui
from critique_bot.engine.chat import (  # noqa: F401
    DEFAULT_COMPACT_AFTER_CHARS,
    DEFAULT_COMPACT_AFTER_TURNS,
    RETRY_DELAYS,
    _Chat,
)
from critique_bot.engine.contracts import AnswerContract, run_read_only_task  # noqa: F401
from critique_bot.engine.dispatch import (  # noqa: F401
    READ_ONLY_TOOLS,
    _execute,
    _Perm,
    command_argv,
    execute_tool,
)
from critique_bot.engine.loop import effective_settings, run_agent_loop  # noqa: F401
from critique_bot.engine.parse import (  # noqa: F401
    ToolCall,
    commentary,
    format_tool_result,
    hide_tool_markup,
    parse_tool_calls,
)
from critique_bot.engine.prompts import (  # noqa: F401
    DEFAULT_TOOL_FORMAT,
    TOOL_FORMAT_NATIVE,
    TOOL_FORMAT_OPENAI,
    _agent_prompt_path,
    _read_sections,
    _web_hosts,
    load_prompt_sections,
    load_role_instructions,
    tool_format_of,
)
from critique_bot.engine.replies import _hands_back, _provider_error  # noqa: F401
from critique_bot.engine.runs import run_agent  # noqa: F401
from critique_bot.engine.setup import (  # noqa: F401
    DEFAULT_RESULT_CHARS,
    MINIMAL_TOOLS,
    _loop_settings,
    _model_label,
    _open_shell_session,
    _remove_old_codegraph,
    _resolve_role,
    _result_budget,
    _shell_preference,
    _tool_setup,
)
from critique_bot.engine.task import _TaskRun  # noqa: F401
from critique_bot.engine.ui import _activity, _show_diff_in_ui, use_ui  # noqa: F401
from critique_bot.prompt_build import seed_message  # noqa: F401
from critique_bot.tool_api import ALLOWED_TOOLS  # noqa: F401

__all__ = [
    "ALLOWED_TOOLS",
    "ToolCall",
    "command_argv",
    "execute_tool",
    "format_tool_result",
    "parse_tool_calls",
    "AnswerContract",
    "READ_ONLY_TOOLS",
    "hide_tool_markup",
    "run_agent",
    "run_agent_loop",
    "run_read_only_task",
]


def __getattr__(name: str) -> Any:
    # ``agent.agent_ui``: the UI installed now (engine.ui.current()).
    if name == "agent_ui":
        return ui.current()
    raise AttributeError(name)
