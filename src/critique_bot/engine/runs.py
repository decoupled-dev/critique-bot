"""A whole agent run: open the backend, set the session up, run the loop, save the transcript.

``run_agent`` is what ``crit`` (and every front end on the Engine API) runs for a session.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import agent_shell, log, model_api, run_stats, taint, ui_api
from critique_bot import session_log as session_log_mod
from critique_bot import tool_api
from critique_bot import tool_api as agent_tools
from critique_bot.config import BotConfig, effective_theme
from critique_bot.engine import loop, ui
from critique_bot.engine import setup as engine_setup
from critique_bot.model_api import ChatError
from critique_bot.output import isoformat, write_output
from critique_bot.engine.context import (
    _announce_session,
    _close_session_log,
    _memory_notes,
    _open_session_log,
    _session_settings,
)
from critique_bot.engine.loop import DEFAULT_HELPER_SESSIONS, MAX_HELPER_SESSIONS, effective_settings
from critique_bot.engine.modes import _MODES
from critique_bot.engine.parse import _STATUS_OK
from critique_bot.engine.prompts import _web_hosts, load_agent_instructions, load_role_instructions
from critique_bot.engine.setup import (
    _available_shells,
    _load_extensions,
    _loop_settings,
    _model_label,
    _prewarm_shell_environment,
    _resolve_role,
    _result_budget,
    _session_start_hook,
    _shell_preference,
    _tool_setup,
    offers_patch,
    without_tool_lines,
)
from critique_bot.engine.text import _int_setting
from critique_bot.engine.ui import _ask_user, _print_status, _ui
from critique_bot.project_home import BotHome, resolve_check_command
from critique_bot.prompt_build import seed_message
from critique_bot.tool_api import ALLOWED_TOOLS


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
    setup = _tool_setup(_loop_settings(config, settings), helpers=helpers, web=bool(_web_hosts(settings)),
                        native_ok=model_api.native_tools_ok(config), extensions=extensions)
    try:
        instructions = load_agent_instructions(setup.tool_format)
        if not offers_patch(_loop_settings(config, settings)):
            instructions = without_tool_lines(instructions, ["apply_patch"])
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
    session_shell = engine_setup._open_shell_session(home.root, shell, available, offline=offline)
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
        engine_setup._remove_old_codegraph()
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


def _loop_kwargs(candidates: dict[str, Any]) -> dict[str, Any]:
    """Only the keyword arguments ``run_agent_loop`` accepts (it gains new ones over time)."""
    import inspect

    try:
        accepted = set(inspect.signature(loop.run_agent_loop).parameters)
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
                    result = loop.run_agent_loop(session, first_task=task, **kwargs)
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


def format_transcript(turns: list[dict[str, str]]) -> str:
    headings = {"user": "You", "assistant": "Assistant", "tool": "Tool"}
    parts = ["# Agent", ""]
    for turn in turns:
        parts.append(f"## {headings.get(turn.get('role', ''), 'Note')}")
        parts.append("")
        parts.append(turn.get("content", "").rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
