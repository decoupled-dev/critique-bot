# SPDX-License-Identifier: Apache-2.0
"""The session loop: tasks in, one :class:`task._TaskRun` each, until the person stops.

``run_agent_loop`` reads the next task from the UI, builds its first message (the task,
the repo map, skills, mode and change notes, the first turn's context), opens an undo
checkpoint, runs the task, and records it in the session log.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from critique_bot import agent_build, agent_edit, agent_shell, code_index, config_prompts, egress, log, prompt_build, roles
from critique_bot import memory_recall
from critique_bot import session_log as session_log_mod
from critique_bot import tool_api
from critique_bot import tool_api as agent_tools
from critique_bot.engine import delegate as delegate_mod
from critique_bot.engine import dispatch, runtime, ui
from critique_bot.engine.chat import (
    BOUNDARY_ROTATION_SHARE,
    DEFAULT_COMPACT_AFTER_CHARS,
    DEFAULT_COMPACT_AFTER_MINUTES,
    DEFAULT_COMPACT_AFTER_TURNS,
    DEFAULT_REPLY_RETRIES,
    _Chat,
    _ProviderFailed,
    _stop_reply,
)
from critique_bot.engine.context import (
    _LoggedHooks,
    _resume_from_log,
    _setup_model_api_session,
    _take_session_change,
    _update_session_meta,
)
from critique_bot.engine.contracts import AnswerContract
from critique_bot.engine.dispatch import _PARALLEL_SAFE, _tool_context
from critique_bot.engine.modes import _MODE_NOTES, _current_mode
from critique_bot.engine.parse import (
    ToolCall,
    _cut_fabricated,
    _hide_tool_markup,
    commentary,
    format_tool_result,
    parse_tool_calls,
)
from critique_bot.engine.prompts import (
    TOOL_FORMAT_NATIVE,
    _setting_list,
    _web_hosts,
    load_prompt_sections,
    load_role_instructions,
    skills_block,
    task_message,
    tool_format_of,
)
from critique_bot.engine.replies import _answer_only, _idle_tool_reply, _keywords, _status_code
from critique_bot.engine.stop_rules import DEFAULT_CHECK_TIMEOUT
from critique_bot.engine.task import _TaskRun
from critique_bot.engine.text import _cap, _int_setting, _one_line
from critique_bot.engine.ui import (
    _ask_user,
    _emit,
    _print_status,
    _read_message,
    _tool_done,
    _tool_output,
    _tool_start,
    _ui,
)
from critique_bot.prompt_build import without_ready as _without_ready
from critique_bot.tool_api import DEFAULT_TOOL_CHARS, TaskState, ToolContext, canonical_tool


_QUIT = {"exit", "quit", "/exit", "/quit", "/q"}


#: Helper chat tabs a task can be split across (the delegate tool). 0 turns it off.
DEFAULT_HELPER_SESSIONS = 2


MAX_HELPER_SESSIONS = 4


REPO_MAP_CHARS = 2_500


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
    ui_mode_at_start = ui._ui_approve_mode()
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
        registry=options.get("_registry"),
    )
    outcome_code = "COMPLETED"
    helpers = delegate_mod.SessionHelpers(
        helper_factory,
        helper_count,
        inline=helper_inline,
        seed=lambda: chat.seed or seed or "",
        sections=sections,
        base=base,
        max_result_chars=max_result_chars,
        retries=_int_setting(options, "reply_retries", DEFAULT_REPLY_RETRIES, 0),
        compact_after=_int_setting(options, "compact_after_chars", DEFAULT_COMPACT_AFTER_CHARS, 10_000),
    )

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
    if first_context and options.get("environment_snapshot") is not False:
        # The first user turn (never the prefix): OS, shell, cwd, git, languages, the test
        # command, tools on PATH, and the background jobs already running.
        first_context = (first_context + "\n\n" + _environment_note(workspace, chosen, session_shell, options)).strip()
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
            delegate=helpers.for_task(cancel, checkpoints, task_state),
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
        hunch = memory_recall.begin_task(workspace, index_path, task, options, slog)  # Stage 8b: project memory
        if hunch:
            message += "\n\n" + hunch
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
        else:
            if options.get("keep_background_jobs") is not True:
                stopped = _stop_task_jobs(session_shell, run.jobs_before)
                if stopped:
                    _ui("note", "Stopped the background jobs this task left running: " + ", ".join(stopped))
        mode_told = run.mode_seen
        changed = list(task_state.edits)
        slog.write("task_finished", code=outcome_code, task=task, changed=changed or None)
        memory_recall.end_task(workspace, index_path, task, outcome_code, changed, list(task_state.reads), options, slog,
                               side_factory=side_factory)  # Stage 8b: episode, cards, patterns
        _update_session_meta(slog)
        runtime.hook("task_finished", {"task": task, "status": outcome_code, "changed": changed})
        chat.history.append(
            f"{_one_line(task, 100)} -> {outcome_code}"
            + (f" (changed {', '.join(changed[:6])}{' ...' if len(changed) > 6 else ''})" if changed else "")
        )
    helpers.close()
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


def _environment_note(workspace: Path, shell: Any, session_shell: Any, options: dict[str, Any]) -> str:
    """tool_api.environment_snapshot for the first turn; "" when it fails (it is a convenience)."""
    try:
        return tool_api.environment_snapshot(workspace, shell=shell, session=session_shell, settings=options)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"environment snapshot failed: {exc}")
        return ""


def _stop_task_jobs(session_shell: Any, before: set[str]) -> list[str]:
    """At task end: stop the background jobs this task started that still run. Their ids.

    Jobs running before the task (an earlier task's server) are left alone. The model was
    asked about running jobs before COMPLETED (JOBS_RUNNING); ``"keep_background_jobs":
    true`` keeps them all.
    """
    if session_shell is None:
        return []
    try:
        rows = list(session_shell.jobs())
    except Exception as exc:  # noqa: BLE001
        log.debug(f"listing background jobs failed: {exc}")
        return []
    stopped: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("running") or row.get("id") in before:
            continue
        try:
            session_shell.kill_background(row.get("id"))
            stopped.append(str(row.get("id")))
        except Exception as exc:  # noqa: BLE001
            log.debug(f"stopping background job {row.get('id')} failed: {exc}")
    return stopped


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
                result = dispatch._execute(call.tool, call.arguments, ctx)
                _tool_done(call, result)
                results.append(result)
        payload = _cap("\n".join(format_tool_result(item) for item in results), max_result_chars)
        turns.append({"role": "tool", "content": payload})


def _load_rules(options: dict[str, Any]) -> Any:
    from critique_bot import agent_rules

    rules, problems = agent_rules.load_rules(options)
    for problem in problems:
        log.warn(f".crit/settings.json: {problem}")
    return rules or None
