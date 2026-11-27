# SPDX-License-Identifier: Apache-2.0
"""Answer contracts and read-only tasks: what an app (the review) gets from the engine.

A read-only task reads the project and git history only; its final answer must pass an
:class:`AnswerContract`, so an app can parse it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from critique_bot.config import BotConfig
from critique_bot.project_home import BotHome


@dataclass(frozen=True)
class AnswerContract:
    """What the final answer of a task must look like, for callers that parse it (a review).

    A reply without tool calls ends the task when ``accepts(reply)`` is true. Otherwise
    ``nudge`` is sent back, up to ``max_nudges`` times, and then the task fails with
    ``failure``. ``last_round`` (when set) goes with the last allowed tool round.
    """

    accepts: Callable[[str], bool]
    nudge: str
    last_round: str = ""
    max_nudges: int = 2
    failure: str = "the final answer never came in the required format"
    note: str = "Asking for the answer in the required format."


def run_read_only_task(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    answer: AnswerContract,
    role: str,
    max_rounds: int,
    headed: bool,
) -> tuple[int, list[dict[str, str]]]:
    """Run one read-only task (a review) and return (exit code, turns).

    The model reads the project and git history with tools; edits, commands, fetches,
    helper chats, and paths outside the project are refused without asking, so no
    prompt waits for a person and the input cannot make crit act. The task ends on a
    reply that ``answer`` accepts. The turns are saved as a transcript in
    ``.crit/sessions`` as an agent run is.
    """
    # Imported here: the task loop imports AnswerContract from this module.
    from critique_bot import agent_shell, log, model_api
    from critique_bot.engine import runs, ui
    from critique_bot.engine.context import _close_session_log, _memory_notes, _open_session_log
    from critique_bot.engine.dispatch import READ_ONLY_TOOLS
    from critique_bot.engine.loop import effective_settings
    from critique_bot.engine.parse import _STATUS_OK
    from critique_bot.engine.prompts import load_agent_instructions, load_role_instructions
    from critique_bot.engine.setup import _loop_settings, _model_label, _result_budget, _shell_preference, _tool_setup
    from critique_bot.engine.ui import _print_status
    from critique_bot.prompt_build import seed_message
    from critique_bot.config import effective_theme

    started = datetime.now(timezone.utc)
    turns: list[dict[str, str]] = []
    outcome = ["COMPLETED"]
    settings = effective_settings(home.settings)
    slog = _open_session_log(home, config, task, settings=settings, kind="review")
    setup = _tool_setup(settings, helpers=0, web=False, only=sorted(READ_ONLY_TOOLS), native_ok=model_api.native_tools_ok(config))
    try:
        instructions = load_agent_instructions(setup.tool_format)
    except OSError as exc:
        log.error(str(exc))
        log.notice(f"Problem: {exc}")
        return 1, turns
    if settings.get("seed_instructions") is False:
        instructions = ""
    shell = agent_shell.detect_shell(preference=_shell_preference(settings))
    ui.current().configure(
        workspace=home.root,
        model=_model_label(config),
        shell=shell,
        approve_mode="plan",
        theme=effective_theme(settings),
        cache_dir=home.cache_dir,
        tools=sorted(READ_ONLY_TOOLS),
        role=role,
    )
    ui.current().install_log_bridge()
    seed = seed_message(
        home.root,
        instructions,
        shell=shell,
        notes=_memory_notes(home, settings),
        helpers=0,
        offline=True,
        role=load_role_instructions(role),
        tools=setup.tools_block,
    )
    code = 1
    try:
        code = runs._run_session(
            config,
            home,
            task,
            headed=headed,
            loop_kwargs={
                "workspace": home.root,
                "index_path": home.index_path,
                "cache_dir": home.cache_dir,
                "max_rounds": max_rounds,
                "max_result_chars": _result_budget(config, settings),
                "seed": seed,
                "outcome": outcome,
                "check_command": None,
                "shell": shell,
                "approve_mode": "plan",
                "turns": turns,
                "settings": {**_loop_settings(config, settings), "tool_format": setup.tool_format,
                             "_native_tools": setup.native_tools},
                "ask_user": lambda _question: "",
                "helper_count": 0,
                "role": role,
                "read_only": True,
                "answer": answer,
                "single_task": True,
                "session_log": slog,
            },
            turns=turns,
        )
    except KeyboardInterrupt:
        outcome[:] = ["INTERRUPTED"]
        _print_status("INTERRUPTED")
        code = 130
    finally:
        ui.current().shutdown()
        runs._save_transcript(config, home, turns, started=started, output_dir=None, folder=slog.folder if slog.enabled else None)
        _close_session_log(slog, outcome[-1])
    if code == 0 and outcome[-1] not in _STATUS_OK:
        code = 1
    return code, turns
