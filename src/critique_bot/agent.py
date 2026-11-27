"""Local coding agent driven through a web chat page.

The chat model prints ``<tool_call>`` blocks. This module parses them, runs
the tool from :mod:`critique_bot.agent_tools`, and sends ``<tool_result>``
blocks back on the same chat, each followed by a short STATE block so the
task stays in view however long the conversation gets. Model-facing text
lives in ``prompts/agent.txt``.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import agent_build, agent_edit, agent_shell, code_index, config_prompts, egress, integrations, log, roles
from critique_bot import taint
from critique_bot import run_stats
from critique_bot import session_log as session_log_mod
from critique_bot import prompt_build
from critique_bot.prompt_build import READY_LINE, seed_message
from critique_bot.prompt_build import without_ready as _without_ready
from critique_bot.config import effective_theme
from critique_bot import tool_api
from critique_bot import tool_api as agent_tools
from critique_bot.tool_api import (
    ALLOWED_TOOLS,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_TOOL_CHARS,
    MUTATING,
    TaskState,
    ToolContext,
    canonical_tool,
    fill,
    is_git_repo,
    normalize_args,
)
from critique_bot.tool_api import execute as _execute
from critique_bot.project_home import BotHome, resolve_check_command
from critique_bot import model_api, ui_api
from critique_bot.engine import runtime, ui
from critique_bot.model_api import COMPLETION_IDLE, ChatError, CloudflareBlocked, ReplyStalled
from critique_bot.config import BotConfig
from critique_bot.output import isoformat, write_output

from critique_bot.engine.parse import (  # noqa: F401
    ToolCall,
    _ARG_KEYS,
    _ATTR_RE,
    _BARE_RESULT_RE,
    _BLOCK_RE,
    _CALL_TAGS,
    _CHROME_LINES,
    _CLOSED_TOOL_MARKUP_RE,
    _FENCED_JSON_RE,
    _FENCE_RE,
    _INNER_ARGS_RE,
    _INNER_NAME_RE,
    _LIST_KEYS,
    _NAME_KEYS,
    _OPEN_RE,
    _QUOTED_CONTEXT_RE,
    _RESULT_OPEN_RE,
    _STATUS_OK,
    _STATUS_WORDS,
    _TAIL_PATH_RE,
    _TAIL_TOOL_RE,
    _TOOL_TAG_RE,
    _TRAILING_PROSE_RE,
    _UNCLOSED_TOOL_RE,
    _WRAPPER_TYPES,
    _WRITE_CALL_RE,
    _WRITE_JSON_HINT,
    _answer_text,
    _bare_json_calls,
    _bare_tool_blobs,
    _call_from_data,
    _calls_from_data,
    _cut_call_label,
    _cut_call_name,
    _cut_fabricated,
    _drop_trailing_prose,
    _hide_tool_markup,
    _json_error,
    _looks_quoted,
    _openai_reply,
    _parse_block,
    _parse_tagged,
    _strip_chrome,
    _whole_tail_calls,
    commentary,
    format_tool_result,
    hide_tool_markup,
    parse_tool_calls,
)
from critique_bot.engine.json_repair import (  # noqa: F401
    _BARE_TOOL_KEY_RE,
    _JSON_STRING_ESCAPES,
    _PATH_LEAD,
    _RAW_WIN_PATH_RE,
    _TRAILING_COMMA_RE,
    _closer_is_json,
    _json_objects,
    _json_value_at,
    _key_follows,
    _loads_lenient,
    _normalize_json_text,
    _raw_path_start,
    _read_json_string,
    _repair_json,
    _string_ends,
)
from critique_bot.engine.replies import (  # noqa: F401
    MAX_HAND_BACKS,
    _ANSWER_TASK_RE,
    _EDIT_TASK_RE,
    _FAIL_HEAD,
    _HAND_BACK_RE,
    _PERMISSION_MARKERS,
    _PLAN_RE,
    _PROMISE_MARKERS,
    _PROMISE_START_RE,
    _PROVIDER_ERROR_MAX,
    _PROVIDER_ERROR_RE,
    _QUESTION_MARKERS,
    _REFUSAL_MARKERS,
    _TEST_MARKERS,
    _UNTAGGED_PLAN_RE,
    _answer_only,
    _asks_for_change,
    _asks_user,
    _call_key,
    _diffstat,
    _failure_reason,
    _hands_back,
    _has_checks,
    _idle_tool_reply,
    _keywords,
    _no_edit_needed,
    _normalized,
    _only_promises,
    _plan_text,
    _provider_error,
    _real_question,
    _refuses,
    _stalls,
    _status_code,
)
from critique_bot.engine.text import (  # noqa: F401
    _cap,
    _one_line,
)

from critique_bot.engine.prompts import (  # noqa: F401
    DEFAULT_TOOL_FORMAT,
    MAX_SKILL_CHARS,
    TOOL_FORMATS,
    TOOL_FORMAT_NATIVE,
    TOOL_FORMAT_OPENAI,
    TOOL_FORMAT_TAGS,
    _FALLBACKS,
    _RESPONSES_WIRE,
    _SECTION_RE,
    _agent_prompt_path,
    _file_agent,
    _read_sections,
    _setting_list,
    _web_hosts,
    load_agent_instructions,
    load_prompt_sections,
    load_role_instructions,
    skills_block,
    task_message,
    tool_format_of,
)

from critique_bot.engine.ui import (  # noqa: F401
    _REVIEW_LINES,
    _activity,
    _approve_prompt,
    _ask_user,
    _call_paths,
    _emit,
    _friendly_error,
    _paint,
    _present_text,
    _print_status,
    _read_message,
    _review_plan,
    _set_active,
    _show_diff_in_ui,
    _tool_done,
    _tool_output,
    _tool_start,
    _ui,
    _ui_approve_mode,
    _ui_result,
    _waiting,
    current,
    use_thread_ui,
    use_ui,
)

from critique_bot.engine.chat import (  # noqa: F401
    AMNESIA_VIOLATIONS,
    BOUNDARY_ROTATION_SHARE,
    CUT_REPLIES_PER_CHAT,
    DEFAULT_COMPACT_AFTER_CHARS,
    DEFAULT_COMPACT_AFTER_MINUTES,
    DEFAULT_COMPACT_AFTER_TURNS,
    DEFAULT_REPLY_RETRIES,
    HEALTH_BAD_REPLIES,
    HEALTH_WINDOW,
    RETRIES_BEFORE_NEW_CHAT,
    RETRY_DELAYS,
    _Chat,
    _ProviderFailed,
    _stop_reply,
)

from critique_bot.engine.context import (  # noqa: F401
    _LoggedHooks,
    _announce_session,
    _close_session_log,
    _compact_at_boundary,
    _memory_notes,
    _open_session_log,
    _pin_slot,
    _resume_from_log,
    _session_settings,
    _setup_model_api_session,
    _side_summarizer,
    _take_session_change,
    _update_session_meta,
)
from critique_bot.engine.dispatch import (  # noqa: F401
    Dispatch,
    LOOP_REPLIES,
    MAX_SAME_CALL,
    PARALLEL_WORKERS,
    READ_ONLY_TOOLS,
    _PARALLEL_SAFE,
    _POLLING,
    _Perm,
    _READ_ONLY_FALLBACK,
    _RULE_ANSWERABLE_RISKS,
    _read_only_allows,
    _read_only_command,
    _read_only_tools,
    _rule_allows,
    _rule_denied,
    _save_allow_rule,
    _tool_context,
    command_argv,
    execute_tool,
)
from critique_bot.engine.modes import (  # noqa: F401
    MAX_PLAN_REVISIONS,
    Modes,
    _MODES,
    _MODE_NOTES,
    _current_mode,
)
from critique_bot.engine.stop_rules import (  # noqa: F401
    DEFAULT_CHECK_TIMEOUT,
    MAX_CHECK_CYCLES,
    MAX_DENIED_ROUNDS,
    MAX_FAILED_ROUNDS,
    MAX_REFUSALS,
    MAX_TRUNCATIONS,
    MAX_USER_QUESTIONS,
    StopRules,
)
from critique_bot.engine.contracts import (  # noqa: F401
    AnswerContract,
)

from critique_bot.engine.task import (  # noqa: F401
    _Hooks,
    _TaskRun,
)

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

_QUIT = {"exit", "quit", "/exit", "/quit", "/q"}
#: Helper chat tabs a task can be split across (the delegate tool). 0 turns it off.
DEFAULT_HELPER_SESSIONS = 2
MAX_HELPER_SESSIONS = 4
#: Characters per tool-result message unless "max_result_chars" says otherwise.
DEFAULT_RESULT_CHARS = 20_000
REPO_MAP_CHARS = 2_500


# --------------------------------------------------------------------------- parsing


# --------------------------------------------------------------------------- reply classification


# --------------------------------------------------------------------------- prompt file


# --------------------------------------------------------------------------- the loop


def _int_setting(settings: dict[str, Any], key: str, default: int, minimum: int) -> int:
    raw = settings.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < minimum:
        return default
    return int(raw)


def effective_settings(settings: object) -> dict[str, Any]:
    """``.crit/settings.json`` as the agent applies it: under strict egress nothing but the chat is reached.

    With ``"egress": "strict"`` in config.json (the default), commands run offline, web_fetch is
    off, and commands that can send data are blocked, whatever the settings file says.
    """
    options = dict(settings) if isinstance(settings, dict) else {}
    if egress.strict():
        options.update({"offline": True, "web_fetch": False, "network_commands": "block"})
    return options


def run_agent_loop(
    session: Any,
    *,
    workspace: Path,
    index_path: Path | None,
    cache_dir: Path | None,
    first_task: str,
    max_rounds: int | None,
    max_result_chars: int,
    seed: str | None = None,
    read_message: Callable[[], str | None] | None = None,
    emit: Callable[[str], None] | None = None,
    outcome: list[str] | None = None,
    check_command: str | None = None,
    shell: agent_shell.Shell | None = None,
    runner: Callable[..., Any] | None = None,
    approve_mode: str = "ask",
    session_shell: Any = None,
    turns: list[dict[str, str]] | None = None,
    ask_user: Callable[[str], str | None] | None = None,
    settings: dict[str, Any] | None = None,
    seed_first: bool = False,
    helper_factory: Callable[[], Any] | None = None,
    helper_count: int = 0,
    helper_inline: bool = False,
    role: str | None = None,
    reseed: Callable[[str], str] | None = None,
    read_only: bool = False,
    answer: AnswerContract | None = None,
    single_task: bool = False,
    session_log: Any = None,
    resume: bool = False,
    side_factory: Callable[[], Any] | None = None,
) -> list[dict[str, str]]:
    """Talk to ``session.send`` until each task reaches a finish code.

    ``seed`` (the instructions) goes in front of the first task, so the first
    reply already works on it; ``seed_first`` sends it alone first instead.
    ``role`` is the role the seed was built for (``"role"`` in settings when
    not given); its skills go with every task. ``reseed(role_id)`` builds the
    seed for another role: after ``/role`` the next task starts a new chat with it.
    Each task gets fresh working memory and an undo checkpoint. COMPLETED,
    FINISHED, DONE, FAILED, BLOCKED, a direct answer to a question, or a plain
    answer after a tool has run ends that task; the same chat waits for the
    next one. Ctrl+C ends the running task as INTERRUPTED; ``/new`` starts a
    new chat. ``turns`` (when given) is filled as the session goes.

    ``settings`` keys: ``check_timeout`` (seconds, default 600),
    ``compact_after_chars`` (default 120000), ``reply_retries`` (default 3).

    ``session_log`` (a :class:`session_log.SessionLog`) records every event of the session;
    with ``resume`` it already holds an earlier run, and the conversation is rebuilt from
    it first. ``side_factory`` opens a second session on the same model for one-off
    requests (the compaction summary). A model API session (``_context_window`` in
    settings, a session with ``replace_history``) is compacted by context pressure
    (:mod:`compaction`) instead of moving to a new chat by size, turns, and time.
    """
    turns = turns if turns is not None else []
    reader = read_message or _read_message
    show = emit or _emit
    ask_person = ask_user or (lambda question: _ask_user(question, None))
    sections = load_prompt_sections(tool_format_of(settings if isinstance(settings, dict) else None))
    native: list[dict[str, Any]] | None = None
    if sections.get("_FORMAT") == TOOL_FORMAT_NATIVE:
        found = (settings or {}).get("_native_tools") if isinstance(settings, dict) else None
        native = list(found) if found else sorted(
            tool_api.registry().openai_tools(), key=lambda tool: str((tool.get("function") or {}).get("name")))
    runtime.start(native=native, extensions_=(settings or {}).get("_extensions") if isinstance(settings, dict) else None)
    _NATIVE_TOOLS = native

    def ask(question: str) -> str | None:
        # Auto mode does not stop for questions: the model decides and says what it chose.
        if decide_in_auto and _current_mode(approve_mode, ui_mode_at_start) == "auto":
            _ui("note", f"Auto mode: the model decides ({_one_line(question, 100)})")
            return sections["AUTO_DECIDE"]
        return ask_person(question)

    workspace = Path(workspace).resolve()
    checkpoints = agent_edit.Checkpoints(cache_dir, workspace)
    chosen = shell or getattr(session_shell, "shell", None) or agent_shell.detect_shell()
    options = effective_settings(settings)
    role_now = roles.normalize_role(role) or roles.normalize_role(options.get("role")) or roles.GENERIC
    chat = _Chat(
        session,
        seed=seed or "",
        retries=_int_setting(options, "reply_retries", DEFAULT_REPLY_RETRIES, 0),
        compact_after=_int_setting(options, "compact_after_chars", DEFAULT_COMPACT_AFTER_CHARS, 10_000),
        compact_turns=_int_setting(options, "compact_after_turns", DEFAULT_COMPACT_AFTER_TURNS, 5),
        compact_minutes=_int_setting(options, "compact_after_minutes", DEFAULT_COMPACT_AFTER_MINUTES, 5),
    )
    if cache_dir is not None:
        chat.failure_dir = Path(cache_dir) / "failures"
    slog = session_log if session_log is not None else session_log_mod.NullLog()
    chat.log = slog
    chat.side_factory = side_factory
    _setup_model_api_session(chat, options, slog)
    handoff_notes = options.get("handoff_notes") is not False
    check_timeout = _int_setting(options, "check_timeout", DEFAULT_CHECK_TIMEOUT, 1)
    build_timeout = _int_setting(options, "build_timeout", int(agent_build.DEFAULT_BUILD_TIMEOUT), 60)
    command_retries = _int_setting(options, "command_retries", 1, 0)
    if session_shell is not None and options.get("background_on_timeout") is False:
        try:
            session_shell.detach_on_timeout = False
        except Exception:
            pass
    confirm_risky = options.get("confirm_risky") is not False
    decide_in_auto = str(options.get("auto_questions") or "decide").lower() != "ask"
    auto_skills = options.get("auto_skills") is not False
    max_skills = _int_setting(options, "max_skills", 2, 0)
    always: set[str] = set()
    ui_mode_at_start = _ui_approve_mode()
    base = dict(
        workspace=workspace,
        index_path=index_path,
        cache_dir=cache_dir,
        max_chars=min(DEFAULT_TOOL_CHARS, max_result_chars),
        runner=runner,
        shell=chosen,
        session=session_shell,
        on_output=_tool_output,
        ask_user=lambda question: ask(question) or "",
        confirm=lambda question: ask_person(question) or "",
        build_timeout=float(build_timeout),
        command_retries=command_retries,
        network_commands=(
            "ask" if str(options.get("network_commands") or "").lower() == "ask" and options.get("offline") is not True else "block"
        ),
        web_hosts=_web_hosts(options),
        rules=_load_rules(options),
        settings=options,
    )
    outcome_code = "COMPLETED"
    pool_box: list[Any] = []

    def make_delegate(
        cancel: threading.Event, checkpoints_now: Any, state: TaskState
    ) -> Callable[[list[dict[str, Any]]], dict[str, Any]] | None:
        """The delegate tool's runner for one task: briefs go to helper tabs, all at once."""
        if helper_factory is None or helper_count < 1:
            return None
        from critique_bot import agent_helpers

        def delegate(briefs: list[dict[str, Any]]) -> dict[str, Any]:
            if not pool_box:
                # Inline: one extra tab on this thread, briefs in turn (no remote debugging needed).
                pool_class = agent_helpers.InlineHelperPool if helper_inline else agent_helpers.HelperPool
                pool_box.append(
                    pool_class(
                        helper_factory,
                        helper_count,
                        seed=chat.seed or seed or "",
                        sections=sections,
                        base=base,
                        max_result_chars=max_result_chars,
                        retries=_int_setting(options, "reply_retries", DEFAULT_REPLY_RETRIES, 0),
                        compact_after=_int_setting(options, "compact_after_chars", DEFAULT_COMPACT_AFTER_CHARS, 10_000),
                    )
                )
            started = time.monotonic()
            _set_active("delegate", len(briefs))
            try:
                reports = pool_box[0].run(
                    briefs,
                    cancel=cancel,
                    progress=lambda line: _tool_output(line + "\n"),
                    context={"checkpoints": checkpoints_now},
                )
            finally:
                _set_active("delegate", 1)
            result = agent_helpers.format_reports(reports, time.monotonic() - started)
            for name in result.get("changed") or []:
                # The coordinator's task owns these changes: the diff, the check, and undo cover them.
                state.edits[name] = state.edits.get(name, 0) + 1
            return result

        return delegate

    carry = ""
    restored = False
    if resume and slog.enabled:
        restored, carry = _resume_from_log(chat, slog)
    if seed and seed.strip():
        slog.prefix = _without_ready(seed) if not seed_first else seed.strip()
        if not restored:
            slog.write("system_prompt", hash=prompt_build.prefix_hash(seed), text=seed)
            if _NATIVE_TOOLS:
                declared = json.dumps(_NATIVE_TOOLS, sort_keys=True, ensure_ascii=False)
                # One string, so the long list goes to a blob instead of a long line in the log.
                slog.write("tools_declared", hash=session_log_mod.text_hash(declared)[:16], tools_json=declared,
                           names=[str((tool.get("function") or {}).get("name")) for tool in _NATIVE_TOOLS])
        if restored:
            pass  # the rebuilt conversation already starts with the instructions
        elif seed_first:
            _seed_session(
                chat, seed.strip(), turns, ctx=_tool_context(**base), max_result_chars=max_result_chars, show=show
            )
        else:
            chat.prefix = _without_ready(seed)
    first_context = str(options.get("_first_turn_context") or "").strip() if not restored else ""
    pending = first_task.strip()
    announced = False
    mode_told = _current_mode(approve_mode, ui_mode_at_start)
    while True:
        if not pending and single_task and turns:
            break  # one task, and it ran: do not wait for another
        if not pending:
            # Between tasks: ask about sites the chat page was blocked from (terminal sessions only).
            config_prompts.offer_blocked_hosts()
            if not announced:
                _ui("note", "Ready. Type a task, or exit.")
                announced = True
            try:
                pending = reader() or ""
            except (KeyboardInterrupt, EOFError):
                break
            if not pending.strip():
                break
        task = pending.strip()
        pending = ""
        if task.lower() == "/theme":
            handler = getattr(ui.current(), "handle_command", None)
            if callable(handler):
                handler("/theme", "")
            continue
        if task.lower() == "/new":
            if not chat.restart("you asked for one"):
                _ui("note", "This chat page cannot open a new conversation; staying in this one.")
            continue
        if task.lower().split(None, 1)[0] in {"/role", "/agent"}:
            # A reader without the slash-command layer: handle it here rather than send it as a task.
            handler = getattr(ui.current(), "handle_command", None)
            if callable(handler):
                handler("/role", task.split(None, 1)[1] if " " in task.strip() else "")
            continue
        if task.lower().split(None, 1)[0] in {"/resume", "/fork", "/sessions"}:
            # A reader without the slash-command layer: the UI records the request; the next task uses it.
            handler = getattr(ui.current(), "handle_command", None)
            if callable(handler):
                handler(task.lower().split(None, 1)[0], task.split(None, 1)[1] if " " in task.strip() else "")
            continue
        switched = _take_session_change(chat, slog)
        if switched is not None:
            slog, switch_note = switched
            carry = (carry + "\n\n" + switch_note).strip() if switch_note else carry
        changed_role = _apply_role_change(chat, reseed)
        if changed_role:
            slog.write("role_changed", old=role_now, new=changed_role)
        role_now = changed_role or role_now
        prompt_hook = runtime.hook("user_prompt", {"prompt": task})
        if prompt_hook is not None and prompt_hook.denied:
            _ui("bad", "A hook stopped this task" + (f": {prompt_hook.message}" if prompt_hook.message else "."))
            continue
        _ui("task", f"Working on: {_one_line(task, 100)}")
        turns.append({"role": "user", "content": task})
        slog.write("user_message", text=task, task=True, mode=_current_mode(approve_mode, ui_mode_at_start),
                   role=role_now)
        if chat.stale_for_next_task():
            chat.restart(chat.rotation_reason(size_only=True, share=BOUNDARY_ROTATION_SHARE) or "the chat grew long")
        repo_map = _prepare_index(workspace, index_path, task)
        if chat.map_sent:
            repo_map = ""
        elif repo_map.strip():
            chat.map_sent = True
        skills = _task_skills(task, workspace, chat, options, auto=auto_skills, limit=max_skills, role=role_now)
        mode_now = _current_mode(approve_mode, ui_mode_at_start)
        mode_note = sections["PLAN_MODE"] if mode_now == "plan" and not _answer_only(task) else ""
        if mode_now != mode_told:
            slog.write("mode_changed", old=mode_told, new=mode_now)
            chat.pending_notes.append(prompt_build.change_note("mode", _MODE_NOTES.get(mode_now, mode_now)))
            mode_told = mode_now
        checkpoints.start_task()
        if checkpoints.task_dir is not None:
            slog.write("checkpoint", undo=checkpoints.task_dir.name)
        cancel = threading.Event()
        task_state = TaskState(task=task)
        ctx = _tool_context(
            **base,
            state=task_state,
            checkpoints=checkpoints,
            cancel=cancel,
            delegate=make_delegate(cancel, checkpoints, task_state),
        )
        run = _TaskRun(
            chat,
            task,
            ctx=ctx,
            sections=sections,
            turns=turns,
            show=show,
            max_rounds=max_rounds,
            max_result_chars=max_result_chars,
            check_command=check_command,
            check_timeout=check_timeout,
            approve_mode=approve_mode,
            always=always,
            ask=ask,
            ui_mode_at_start=ui_mode_at_start,
            confirm_risky=confirm_risky,
            read_only=read_only,
            answer=answer,
        )
        if read_only or mode_now != "plan" or _answer_only(task):
            run.plan_done = True
        run.handoff = handoff_notes
        tickets = tool_api.jira_context(task)
        message = task_message(
            task, repo_map=repo_map, skills=skills, mode_note=mode_note, prefix=sections["TASK_PREFIX"], tickets=tickets,
        )
        if chat.pending_notes:
            message = "\n\n".join(chat.pending_notes) + "\n\n" + message
            chat.pending_notes = []
        if first_context:
            message = first_context + "\n\n" + message
            first_context = ""
        if carry:
            message = carry + "\n\n" + message
            carry = ""
        if slog.enabled:
            run.hooks = _LoggedHooks(run.hooks, slog)
        if prompt_hook is not None and prompt_hook.append_context:
            message += "\n\nCONTEXT FROM A HOOK\n" + prompt_hook.append_context
        try:
            outcome_code = run.run(message)
        except KeyboardInterrupt:
            cancel.set()
            _stop_background(session_shell)
            try:
                _stop_reply(chat.session)
            except KeyboardInterrupt:  # a second Ctrl+C: skip it; the next send stops it
                pass
            outcome_code = "INTERRUPTED"
            turns.append({"role": "assistant", "content": "INTERRUPTED by the user"})
            try:
                run._show_disk_once()
            except Exception as exc:  # the diff is a courtesy; the prompt must come back
                log.debug(f"diff after interrupt failed: {exc}")
            _print_status(outcome_code)
            carry = (
                "The user interrupted the previous task. Stop working on it; "
                "its last reply was not acted on. The new task follows."
            )
        mode_told = run.mode_seen
        changed = list(task_state.edits)
        slog.write("task_finished", code=outcome_code, task=task, changed=changed or None)
        _update_session_meta(slog)
        runtime.hook("task_finished", {"task": task, "status": outcome_code, "changed": changed})
        chat.history.append(
            f"{_one_line(task, 100)} -> {outcome_code}"
            + (f" (changed {', '.join(changed[:6])}{' ...' if len(changed) > 6 else ''})" if changed else "")
        )
    for pool in pool_box:
        try:
            pool.close()
        except Exception as exc:  # the session is over; a stuck tab must not hang the exit
            log.debug(f"closing helper tabs failed: {exc}")
    runtime.finish()
    if outcome is not None:
        outcome[:] = [outcome_code]
    return turns


def _apply_role_change(chat: Any, reseed: Callable[[str], str] | None) -> str | None:
    """After ``/role``, the next task goes to a new chat seeded for that role. Returns the new role id."""
    taker = getattr(ui.current(), "take_role_change", None)
    if not callable(taker):
        return None
    try:
        changed = taker()
    except Exception:
        return None
    if not changed:
        return None
    if getattr(chat, "compactor", None) is not None:
        # A model API conversation is append-only (its prefix is cached): the new role's
        # instructions go in as a note with the next message instead of a new chat.
        text = load_role_instructions(changed)
        chat.pending_notes.append(
            prompt_build.change_note("role", f"{roles.role_label(changed)}." + (f"\n{text}" if text else ""))
        )
        return changed
    if reseed is None:
        return changed
    try:
        chat.seed = (reseed(changed) or "").strip()
    except Exception as exc:  # the old seed still works
        log.warn(f"could not rebuild the instructions for role {changed}: {exc}")
        return changed
    if chat.restart(f"the role is now {roles.role_label(changed)}"):
        return changed
    # No new chat: the next message carries the new instructions in front of it.
    chat.prefix = _without_ready(chat.seed) if chat.seed else ""
    _ui("note", "This chat page cannot open a new conversation; the new role goes in front of the next task.")
    return changed


def _task_skills(
    task: str,
    workspace: Path,
    chat: Any,
    settings: dict[str, Any],
    *,
    auto: bool,
    limit: int,
    role: str | None = None,
) -> str:
    """The SKILLS block for this task: pinned skills, the role's skills, and the ones that match, each sent once per chat."""
    select = getattr(agent_tools, "select_skills", None)
    if not callable(select):
        return ""
    pinned = _setting_list(settings, "skills")
    pinned += [name for name in roles.role_skills(role if role is not None else settings.get("role")) if name not in pinned]
    getter = getattr(ui.current(), "pinned_skills", None)
    if callable(getter):
        try:
            pinned += [name for name in getter() if name not in pinned]
        except Exception:
            pass
    try:
        chosen = select(task, workspace, pinned=pinned, limit=limit if auto else 0)
    except Exception as exc:  # skills help; they must never stop a task
        log.debug(f"choosing skills failed: {exc}")
        return ""
    sent = getattr(chat, "skills_sent", None)
    if not isinstance(sent, set):
        sent = set()
        try:
            chat.skills_sent = sent
        except Exception:
            pass
    fresh = [item for item in chosen if item["name"] not in sent]
    if chosen:
        _ui("note", "Skills: " + ", ".join(item["name"] for item in chosen))
    block = skills_block(fresh)
    if block:
        sent.update(item["name"] for item in fresh)
    elif chosen:
        block = "SKILLS: " + ", ".join(item["name"] for item in chosen) + " (sent earlier in this chat; still apply)."
    return block


def _stop_background(session_shell: Any) -> None:
    """Kill the commands still running in the shell session (Ctrl+C)."""
    if session_shell is None:
        return
    try:
        jobs = session_shell.jobs()
    except Exception as exc:
        log.debug(f"listing background jobs failed: {exc}")
        return
    for job in jobs or []:
        if isinstance(job, dict) and job.get("running"):
            try:
                session_shell.kill_background(job.get("id"))
            except Exception as exc:
                log.debug(f"killing background job failed: {exc}")


def _remove_old_codegraph() -> None:
    """Delete the CodeGraph copy earlier versions unpacked (crit's own folder; nothing else uses it)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        folder = base / "critique-bot" / "codegraph"
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "Application Support" / "critique-bot" / "codegraph"
    else:
        folder = Path.home() / ".config" / "critique-bot" / "codegraph"
    if not folder.is_dir():
        return
    try:
        shutil.rmtree(folder)
        log.info(f"removed the old CodeGraph copy at {folder}")
    except OSError as exc:
        log.debug(f"could not remove {folder}: {exc}")


def _prepare_index(workspace: Path, index_path: Path | None, task: str) -> str:
    """Bring the index up to date with the disk and build the repo map."""
    if index_path is None or not Path(index_path).is_file():
        return ""
    try:
        code_index.refresh_index(workspace, index_path)
        return code_index.repo_map(index_path, keywords=_keywords(task), budget_chars=REPO_MAP_CHARS)
    except Exception as exc:  # the map is a hint; a broken index must not stop the task
        log.debug(f"index refresh failed: {exc}")
        return ""


#: Tools the reply to the instructions may run before there is a task.
_SEED_TOOLS = _PARALLEL_SAFE | {"skill"}


def _seed_needs_approval(name: str, call: ToolCall, ctx: ToolContext) -> bool:
    """True when a read-only call would still need the user (a path outside the workspace)."""
    check = getattr(agent_tools, "permission_for", None)
    if check is None:
        return False
    try:
        return (getattr(check(name, call.arguments, ctx), "kind", "read") or "read") != "read"
    except Exception:
        return True


def _seed_session(
    chat: Any,
    seed: str,
    turns: list[dict[str, str]],
    *,
    ctx: ToolContext,
    max_result_chars: int,
    show: Callable[[str], None],
) -> None:
    """Send the tool instructions alone. Read-only tool calls in the reply still run.

    Nothing that changes files or runs a command runs before there is a
    task, whatever name the model uses for it (write_file, patch, bash).
    """
    if not isinstance(chat, _Chat):
        chat = _Chat(chat)
    _ui("note", "Getting ready.")
    payload = seed
    turns.append({"role": "user", "content": seed})
    for _ in range(3):
        try:
            reply = chat.send(payload)
        except _ProviderFailed as exc:
            _ui("bad", str(exc))
            return
        reply, _fabricated = _cut_fabricated(reply)
        calls, unclosed = parse_tool_calls(reply)
        note = commentary(reply)
        if note and _status_code(note) is None and note.strip().upper() != "READY":
            visible = _hide_tool_markup(note)
            if visible:
                show(visible)
        turns.append({"role": "assistant", "content": reply})
        if unclosed or _idle_tool_reply(chat.last_detail, reply):
            payload = format_tool_result(
                {"tool": "", "ok": False, "error": "reply was truncated before the tool call closed; resend complete tool_call blocks"}
            )
            turns.append({"role": "tool", "content": payload})
            continue
        if not calls:
            return
        results = []
        for call in calls:
            name = canonical_tool(call.tool)
            if call.error:
                results.append({"tool": call.tool, "ok": False, "error": call.error})
            elif name and (name not in _SEED_TOOLS or _seed_needs_approval(name, call, ctx)):
                results.append(
                    {"tool": name, "ok": False, "error": "no task yet; wait for the task before changing files or running commands"}
                )
            else:
                _tool_start(call)
                result = _execute(call.tool, call.arguments, ctx)
                _tool_done(call, result)
                results.append(result)
        payload = _cap("\n".join(format_tool_result(item) for item in results), max_result_chars)
        turns.append({"role": "tool", "content": payload})


def run_agent(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    max_rounds: int | None,
    output_dir: Path | None,
    headed: bool,
    approve_mode: str | None = None,
    role: str | None = None,
    resume: str | None = None,
) -> int:
    """Open the Edge session, run the tool loop, and write the transcript.

    ``approve_mode`` is ``"ask"``, ``"edits"``, ``"auto"`` (``--yes``), or ``"plan"``
    (``--plan``); ``None`` reads ``"permissions"`` from ``.crit/settings.json``. ``role`` is the
    persona for this run (``--role``); ``None`` reads ``"role"`` from the settings, and Generic
    is the default. The transcript is saved even when Ctrl+C or an error ends the session.

    Every session is logged in ``.crit/sessions/<id>/`` (session_log). ``resume`` is the id
    (or a prefix, or ``last``) of a logged session to continue instead of starting a new one.
    """
    started = datetime.now(timezone.utc)
    run_stats.reset()
    turns: list[dict[str, str]] = []
    outcome = ["COMPLETED"]
    settings = effective_settings(home.settings)
    try:
        slog = _open_session_log(home, config, task, resume=resume, settings=settings)
    except session_log_mod.SessionNotFound as exc:
        log.notice(f"Problem: {exc}")
        return 1
    helpers = min(_int_setting(settings, "helper_sessions", DEFAULT_HELPER_SESSIONS, 0), MAX_HELPER_SESSIONS)
    extensions = _load_extensions(home.root, settings)
    setup = _tool_setup(settings, helpers=helpers, web=bool(_web_hosts(settings)), native_ok=model_api.native_tools_ok(config),
                        extensions=extensions)
    try:
        instructions = load_agent_instructions(setup.tool_format)
    except OSError as exc:
        log.error(str(exc))
        log.notice(f"Problem: {exc}")
        if extensions is not None:
            extensions.close()
        return 1
    if settings.get("seed_instructions") is False:
        instructions = ""
    role_id = _resolve_role(role, settings, home.root)
    if approve_mode not in _MODES:
        approve_mode = ui_api.normalize_mode(settings.get("permissions") or settings.get("mode"))
    notes = home.project_notes()
    check_command = resolve_check_command(settings, notes)
    memory = _memory_notes(home, settings)
    if helpers and config.max_parallel_tabs < helpers + 1:
        # Extra tabs are opened over the browser's remote debugging port, which this turns on.
        config = dataclasses.replace(config, max_parallel_tabs=helpers + 1)
    _prewarm_shell_environment()
    shell = agent_shell.detect_shell(preference=_shell_preference(settings))
    available = _available_shells()
    offline = settings.get("offline") is True
    session_shell = _open_shell_session(home.root, shell, available, offline=offline)
    sandbox_note = agent_shell.use_sandbox(session_shell, settings, default=getattr(config, "sandbox", None), offline=offline)
    taint.start_session(untrusted=settings.get("untrusted_workspace") is True, workspace=home.root)
    ui.current().configure(
        workspace=home.root,
        model=_model_label(config),
        shell=shell,
        approve_mode=approve_mode,
        theme=effective_theme(settings),
        history_path=home.bot_dir / "history",
        cache_dir=home.cache_dir,
        session_shell=session_shell,
        tools=setup.names or (setup.registry.names() if setup.registry is not None else ALLOWED_TOOLS),
        role=role_id,
    )
    ui.current().welcome_header()
    for line in (extensions.notes if extensions is not None else []):
        _ui("note", line)
    started_hook = _session_start_hook(extensions, setup)
    if started_hook:
        notes = (notes + "\n\n" + started_hook).strip()
    ui.current().install_log_bridge()
    if sandbox_note:
        _ui("work", sandbox_note)
    _announce_session(slog)
    try:
        skill_names = [item["name"] for item in agent_tools.discover_skills(home.root)]
    except Exception as exc:
        log.debug(f"listing skills failed: {exc}")
        skill_names = []

    def build_seed(chosen_role: str) -> str:
        return seed_message(
            home.root,
            instructions,
            shell=shell,
            notes=memory,
            available=available,
            skills=skill_names,
            helpers=helpers,
            web_hosts=_web_hosts(settings),
            offline=settings.get("offline") is True,
            role=load_role_instructions(chosen_role, home.root),
            tools=setup.tools_block,
        )

    seed = build_seed(role_id)
    code = 1
    previous_registry = tool_api.use_registry(setup.registry) if setup.registry is not None else None
    try:
        _remove_old_codegraph()
        code = _run_session(
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
                "check_command": check_command,
                "shell": shell,
                "approve_mode": approve_mode,
                "session_shell": session_shell,
                "turns": turns,
                "settings": {**_loop_settings(config, settings), "tool_format": setup.tool_format,
                             "_native_tools": setup.native_tools, "_extensions": extensions, **_session_settings(config, home)},
                "ask_user": _ask_user,
                "helper_count": helpers,
                "role": role_id,
                "reseed": build_seed,
                "session_log": slog,
                "resume": bool(resume),
            },
            turns=turns,
        )
    except KeyboardInterrupt:
        outcome[:] = ["INTERRUPTED"]
        ui.current().shutdown()
        _print_status("INTERRUPTED")
        code = 130
    finally:
        if previous_registry is not None:
            tool_api.use_registry(previous_registry)
        if extensions is not None:
            extensions.close()
        ui.current().shutdown()
        if session_shell is not None:
            try:
                session_shell.close()
            except Exception as exc:
                log.warn(f"closing the shell session failed: {exc}")
        _save_transcript(
            config,
            home,
            turns,
            started=started,
            output_dir=output_dir,
            commands=list(getattr(session_shell, "history", []) or []),
            folder=slog.folder if slog.enabled else None,
        )
        _close_session_log(slog, outcome[-1] if code in (0, 1) else ("INTERRUPTED" if code == 130 else "FAILED"))
    if code != 0:
        return code
    return 0 if outcome[-1] in _STATUS_OK else 1


#: The "minimal" tool set: the shell does the searching, listing, and git; files are read and
#: edited with tools that number lines, check syntax, and can be undone.
MINIMAL_TOOLS = (
    "run_command",
    "command_output",
    "command_input",
    "kill_command",
    "read_files",
    "edit_file",
    "write_files",
    "todo",
    "ask_user",
)


@dataclass(frozen=True)
class _ToolSetup:
    tool_format: str
    #: The tools block for the first message ("" with the tag format, whose prompt lists the tools).
    tools_block: str
    #: The registry to install for the session, or None for the built-in one.
    registry: Any
    #: Native format: the tools in OpenAI chat format, handed to each session (use_tools).
    native_tools: list[dict[str, Any]] | None = None
    #: The tools the model is told about (for /tools); () when that is the registry's list.
    names: tuple[str, ...] = ()


def _tool_setup(
    settings: dict[str, Any],
    *,
    helpers: int,
    web: bool,
    only: Iterable[str] | None = None,
    native_ok: bool = False,
    extensions: Any = None,
) -> _ToolSetup:
    """The tool format, the tool set, and the tools block, from ``tool_format`` and ``tool_set``.

    ``native_ok``: the backend calls tools natively (a local model server). Then native is the
    default; ``"tool_format": "openai"`` or ``"tags"`` still picks a text format.
    """
    raw = str(settings.get("tool_format") or "").strip().lower()
    fmt = tool_format_of(settings, DEFAULT_TOOL_FORMAT)
    if native_ok and raw in {"", "auto"}:
        fmt = TOOL_FORMAT_NATIVE
    elif fmt == TOOL_FORMAT_NATIVE and not native_ok:
        fmt = TOOL_FORMAT_OPENAI  # the web chat has no native tool calls
    minimal = str(settings.get("tool_set") or "").strip().lower() == "minimal"
    names = list(only) if only is not None else list(MINIMAL_TOOLS if minimal else ALLOWED_TOOLS)
    if helpers < 1 and "delegate" in names:
        names.remove("delegate")
    if not web and "web_fetch" in names:
        names.remove("web_fetch")
    registry = tool_api.builtin_registry().only(names) if (minimal or only is not None) else None
    block = ""
    native: list[dict[str, Any]] | None = None
    source = registry or tool_api.builtin_registry().only(names)
    if extensions is not None and extensions.active():
        # Manifest and MCP tools, hooks, and the agents delegate can start: one merged registry
        # (extensions/session.py), decided now and kept for the whole session.
        source = extensions.registry(source)
        registry = extensions.registry(registry or tool_api.builtin_registry())
        if fmt == TOOL_FORMAT_TAGS:
            block = extensions.tools_text()
    if fmt == TOOL_FORMAT_OPENAI:
        from critique_bot import openai_format

        block = openai_format.tools_block(source.openai_tools())
    elif fmt == TOOL_FORMAT_NATIVE:
        # Sorted by name, so the request's tools are byte-identical turn after turn (prompt cache).
        native = sorted(source.openai_tools(), key=lambda tool: str((tool.get("function") or {}).get("name")))
    names_now = source.names() if extensions is not None and extensions.active() else ()
    return _ToolSetup(fmt, block, registry, native, names_now)


def _load_extensions(root: Path, settings: dict[str, Any]) -> Any:
    """This session's extension tools, MCP servers, hooks and agents (extensions.load_session); None on failure."""
    from critique_bot import extensions

    try:
        return extensions.load_session(root, settings, builtin_names=ALLOWED_TOOLS)
    except Exception as exc:  # extensions are optional: crit runs without them
        log.warn(f"extensions not loaded: {exc}")
        return None


def _session_start_hook(extensions: Any, setup: _ToolSetup) -> str:
    """Run the session_start hooks; the context they add goes into the project notes."""
    if extensions is None:
        return ""
    try:
        decision = extensions.run_hook("session_start", {"tools": list(setup.names or ())})
    except Exception as exc:
        log.warn(f"session_start hook failed: {exc}")
        return ""
    return decision.append_context


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
        code = _run_session(
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
        _save_transcript(config, home, turns, started=started, output_dir=None, folder=slog.folder if slog.enabled else None)
        _close_session_log(slog, outcome[-1])
    if code == 0 and outcome[-1] not in _STATUS_OK:
        code = 1
    return code, turns


def _model_label(config: BotConfig) -> str:
    """What the header and /status call the model: the name, and where it runs when local."""
    return model_api.model_label(config)


def _load_rules(options: dict[str, Any]) -> Any:
    from critique_bot import agent_rules

    rules, problems = agent_rules.load_rules(options)
    for problem in problems:
        log.warn(f".crit/settings.json: {problem}")
    return rules or None


def _loop_settings(config: BotConfig, settings: dict[str, Any]) -> dict[str, Any]:
    """The settings the loop reads, with chat-size defaults that fit a local model's context window.

    The browser defaults (a 300000-character chat, 40000-character tool results) assume a
    hosted model with a large context. A local model has ``local.num_ctx`` tokens; a chat
    bigger than that is silently truncated, so a new chat starts well before that.
    """
    num_ctx = model_api.context_tokens(config)
    if num_ctx is None:
        return settings
    merged = dict(settings)
    # about 3 characters per token of code; leave room for the reply and the instructions
    merged.setdefault("compact_after_chars", max(10_000, int(num_ctx * 2)))
    merged.setdefault("max_result_chars", max(4_000, min(DEFAULT_RESULT_CHARS, int(num_ctx / 2))))
    return merged


def _resolve_role(flag: str | None, settings: dict[str, Any], root: Path | None = None) -> str:
    """The role id for this run: ``--role``, then ``"role"`` in settings, then Generic. Unknown names warn.

    The name of a file-defined agent (``/agent``) counts too: its instructions become the role.
    """
    for source, raw in (("--role", flag), ('settings "role"', settings.get("role"))):
        if raw is None or str(raw).strip() == "":
            continue
        found = roles.normalize_role(raw)
        if found is not None:
            return found
        if root is not None and _file_agent(raw, root) is not None:
            return str(raw).strip()
        log.warn(f"{source}: {raw!r} is not one of {', '.join(roles.ROLE_IDS)}; using {roles.GENERIC}")
    return roles.GENERIC


_SHELL_SETTINGS = ("auto", "pwsh", "powershell", "cmd", "bash", "sh", "zsh", "gitbash", "git-bash")


def _shell_preference(settings: dict[str, Any]) -> str:
    """The ``"shell"`` setting (auto, pwsh, powershell, cmd, bash, sh, zsh); auto when unset or unknown."""
    value = str(settings.get("shell") or "auto").strip().lower()
    if value not in _SHELL_SETTINGS:
        log.warn(f'settings "shell": {value!r} is not one of {", ".join(_SHELL_SETTINGS[:7])}; using auto')
        return "auto"
    return value


def _prewarm_shell_environment() -> None:
    """Start reading the login-shell environment now, so the first command does not wait."""
    try:
        agent_shell.prewarm_login_environment()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"login environment: {exc}")


def _available_shells() -> dict[str, agent_shell.Shell]:
    try:
        return agent_shell.available_shells()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"listing shells failed: {exc}")
        return {}


def _open_shell_session(
    workspace: Path,
    shell: agent_shell.Shell,
    available: dict[str, agent_shell.Shell] | None = None,
    *,
    offline: bool = False,
) -> Any:
    """A persistent ShellSession when agent_shell provides one, else ``None``."""
    factory = getattr(agent_shell, "ShellSession", None)
    if factory is None:
        return None
    try:
        return factory(workspace, shell, available=available, offline=offline)
    except TypeError:
        return factory(workspace, shell, available=available)
    except Exception as exc:
        log.warn(f"shell session unavailable: {exc}")
        return None


def _loop_kwargs(candidates: dict[str, Any]) -> dict[str, Any]:
    """Only the keyword arguments ``run_agent_loop`` accepts (it gains new ones over time)."""
    import inspect

    try:
        accepted = set(inspect.signature(run_agent_loop).parameters)
    except (TypeError, ValueError):
        return candidates
    return {key: value for key, value in candidates.items() if key in accepted}


def _run_session(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    headed: bool,
    loop_kwargs: dict[str, Any],
    turns: list[dict[str, str]],
) -> int:
    """One browser session. A Cloudflare block in headless mode retries once, headed."""
    from critique_bot.model_api import ModelError, open_provider

    kwargs = _loop_kwargs(loop_kwargs)
    try:
        with open_provider(config, headed=headed) as provider:
            with provider.session() as session:
                if kwargs.get("helper_count") and getattr(provider, "can_parallelize", False):
                    kwargs["helper_factory"] = lambda: provider.session(isolated=True)
                elif kwargs.get("helper_count"):
                    # Without remote debugging the helpers take turns in one extra tab on this thread.
                    log.info("helper chats run one at a time in a second tab (no remote debugging)")
                    kwargs["helper_factory"] = lambda: provider.session()
                    kwargs["helper_inline"] = True
                if config.is_local and "side_factory" in _loop_kwargs({"side_factory": None}):
                    # One-off requests on the same model (the compaction summary): another session.
                    kwargs["side_factory"] = lambda: provider.session()
                try:
                    result = run_agent_loop(session, first_task=task, **kwargs)
                    if "turns" not in kwargs and result is not turns:
                        turns[:] = list(result or [])
                except Exception:
                    model_api.save_failure(session, home.sessions_dir)
                    raise
    except ChatError as exc:
        if not headed and "Cloudflare" in str(exc):
            _ui("note", "The headless window was blocked. Opening a visible browser.")
            return _run_session(config, home, task, headed=True, loop_kwargs=loop_kwargs, turns=turns)
        log.error(str(exc))
        ui.current().shutdown()
        log.notice(f"Problem: {exc}")
        return 1
    except ModelError as exc:
        log.error(str(exc))
        ui.current().shutdown()
        log.notice(f"Problem: {exc}")
        return 1
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log.exception(f"unexpected failure: {exc}")
        ui.current().shutdown()
        log.notice(f"crit stopped: {exc}. {log.details_hint()}")
        return 1
    return 0


def _save_transcript(
    config: BotConfig,
    home: BotHome,
    turns: list[dict[str, str]],
    *,
    started: datetime,
    output_dir: Path | None,
    commands: list[dict[str, Any]] | None = None,
    folder: Path | None = None,
) -> None:
    """``agent.json`` / ``agent.md`` for this run: in the session's folder (``folder``), else in a
    folder named after the start time. ``crit eval`` reads ``turns`` and ``stats`` from it."""
    if not turns:
        return
    finished = datetime.now(timezone.utc)
    # The transcript holds what went to the chat: secrets removed, as they were from the messages.
    turns = [{**turn, "content": agent_tools.redact_secrets(str(turn.get("content") or ""))[0]} for turn in turns]
    body = format_transcript(turns)
    payload = {
        "mode": "agent",
        "model": config.model,
        "backend": config.backend,
        "url": config.url,
        "workspace": str(home.root),
        "response": body,
        "turns": turns,
        "started_at": isoformat(started),
        "finished_at": isoformat(finished),
    }
    if commands:
        payload["commands"] = commands
    payload["stats"] = run_stats.snapshot()
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        write_output(folder or home.sessions_dir / stamp, body, payload, stem="agent", print_body=False)
        if output_dir is not None:
            write_output(output_dir, body, payload, stem="agent", print_body=False)
    except OSError as exc:
        log.warn(f"could not save the transcript: {exc}")


def _result_budget(config: BotConfig, settings: dict[str, Any]) -> int:
    """Characters per message sent back to the chat. Smaller keeps the task in view."""
    raw = _loop_settings(config, settings).get("max_result_chars")
    if isinstance(raw, int) and raw >= 4_000:
        return raw
    return min(DEFAULT_RESULT_CHARS, config.max_prompt_chars)


def format_transcript(turns: list[dict[str, str]]) -> str:
    headings = {"user": "You", "assistant": "Assistant", "tool": "Tool"}
    parts = ["# Agent", ""]
    for turn in turns:
        parts.append(f"## {headings.get(turn.get('role', ''), 'Note')}")
        parts.append("")
        parts.append(turn.get("content", "").rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- permission rules


def __getattr__(name: str) -> Any:
    # ``agent.agent_ui``: the UI installed now (engine.ui.current()).
    if name == "agent_ui":
        return ui.current()
    raise AttributeError(name)
