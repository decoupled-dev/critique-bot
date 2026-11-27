"""The hooks between the loop and session_log, compaction and prompt_build (Stage 3).

Logging tool calls, compaction at turn boundaries, resume from the log, switching to
another session's log (/resume, /fork), and the session folder's meta.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from critique_bot import log, model_api, prompt_build, run_stats
from critique_bot import session_log as session_log_mod
from critique_bot.config import BotConfig
from critique_bot.engine import runtime, ui
from critique_bot.engine.chat import _Chat
from critique_bot.engine.parse import ToolCall
from critique_bot.engine.text import _one_line
from critique_bot.engine.ui import _activity, _call_paths, _ui
from critique_bot.project_home import BotHome
from critique_bot.prompt_build import without_ready as _without_ready
from critique_bot.tool_api import MUTATING, canonical_tool


class _LoggedHooks:
    """A task run's hooks that also record tool calls in the session log."""

    def __init__(self, inner: Any, slog: Any) -> None:
        self.inner = inner
        self.slog = slog

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def ui(self, kind: str, message: str) -> None:
        self.inner.ui(kind, message)

    def status(self, code: str) -> None:
        self.inner.status(code)

    def tool_start(self, call: ToolCall) -> None:
        self.slog.write(
            "tool_started",
            tool=canonical_tool(call.tool) or call.tool,
            call_id=call.call_id or None,
            arguments=call.arguments,
            summary=_activity(call),
        )
        self.inner.tool_start(call)

    def tool_done(self, call: ToolCall, result: dict[str, Any]) -> None:
        name = canonical_tool(call.tool) or call.tool
        ok = bool(result.get("ok"))
        text = str(result.get("output") if ok else result.get("error") or "") or ""
        paths = _call_paths(call)
        self.slog.write(
            "tool_result",
            tool=name,
            call_id=call.call_id or None,
            ok=ok,
            summary=_one_line(text, 200),
            path=paths or None,
            mutating=(name in MUTATING) or None,
            denied=bool(result.get("denied")) or None,
            result=result,
        )
        self.inner.tool_done(call, result)


def _setup_model_api_session(chat: _Chat, options: dict[str, Any], slog: Any) -> None:
    """For a model API session: compaction by context pressure, and llama.cpp slot pinning.

    ``_context_window`` in ``options`` is the backend's context window (run_agent sets it for
    a local server). The web chat has none and keeps its size/turn/time rotation.
    """
    from critique_bot import compaction

    session = chat.session
    window = options.get("_context_window")
    if (
        isinstance(window, int)
        and not isinstance(window, bool)
        and window > 0
        and callable(getattr(session, "replace_history", None))
        and options.get("compaction") is not False
    ):
        from critique_bot.redaction import redact_outgoing

        chat.compactor = compaction.Compactor(
            compaction.Policy.from_settings(window, options),
            summarize=_side_summarizer(chat),
            put_blob=lambda text: chat.log.put_blob(text),
            redact=lambda text: redact_outgoing(text, report=False),
        )
    _pin_slot(session, slog.id if slog.enabled else "", options.get("slot_pinning"))


def _pin_slot(session: Any, key: str, setting: Any) -> int | None:
    """llama.cpp: keep the session in one slot (``id_slot``). Off with ``"slot_pinning": false``;
    a number is the server's slot count (no detection)."""
    pin = getattr(session, "pin_slot", None)
    if not callable(pin) or not key or setting is False:
        return None
    slots = setting if isinstance(setting, int) and not isinstance(setting, bool) and setting > 0 else None
    try:
        slot = pin(key, slots)
    except Exception as exc:  # the cache is an optimization; never stop a session over it
        log.debug(f"slot pinning failed: {exc}")
        return None
    if slot is not None:
        log.info(f"llama.cpp: this session uses slot {slot} (id_slot) so its prompt cache stays warm")
    return slot


def _side_summarizer(chat: _Chat) -> Callable[[list[dict[str, Any]], str], str] | None:
    """The compaction summary as one request on a second session that holds the same messages.

    The messages share the conversation's prefix, so a server with a prompt cache (and the
    same llama.cpp slot) reuses it. None without a second session.
    """
    if chat.side_factory is None:
        return None
    from critique_bot import compaction

    def summarize(messages: list[dict[str, Any]], prompt: str) -> str:
        assert chat.side_factory is not None
        handle = chat.side_factory()
        enter = getattr(handle, "__enter__", None)
        side = enter() if callable(enter) and not hasattr(handle, "send") else handle
        try:
            native = runtime.native_tools()
            if native and callable(getattr(side, "use_tools", None)):
                side.use_tools(native)
            _pin_slot(side, chat.log.id if chat.log.enabled else "", None)
            replace = getattr(side, "replace_history", None)
            if callable(replace):
                replace(messages)
                reply = side.send(prompt)
            else:
                rendered = "\n\n".join(f"[{m.get('role')}]\n{m.get('content') or ''}" for m in messages)
                reply = side.send(rendered + "\n\n" + prompt)
        finally:
            close = getattr(handle, "__exit__", None) if handle is not side else None
            try:
                if callable(close):
                    close(None, None, None)
                elif callable(getattr(side, "close", None)):
                    side.close()
            except Exception as exc:  # noqa: BLE001
                log.debug(f"closing the summary session failed: {exc}")
        return compaction.reply_text(reply)

    return summarize


def _compact_at_boundary(run: "_TaskRun") -> bool:
    """At a turn boundary of a model API session: compact when context pressure calls for it."""
    from critique_bot import compaction

    chat = run.chat
    if chat.compactor is None or chat.messages is None:
        return False
    detail = chat.last_detail if isinstance(chat.last_detail, dict) else {}
    used = int(detail.get("prompt_eval_count") or 0) + int(detail.get("eval_count") or 0)
    state = run.state
    facts = {
        "notes": "; ".join(run.findings),
        "changed": ", ".join(state.edits),
        "problems": run.last_output.strip()[-800:] if run.last_failed else "",
        "failing": f"{state.last_command} -> {state.last_exit}" if state.last_command and run.last_failed else "",
    }
    ctx = compaction.Context(
        used_tokens=used,
        pending_chars=len(run.payload),
        turn=chat.replies,
        task=run.task,
        todos=list(state.todos),
        recent_files=list(state.reads),
        workspace=run.ctx.workspace,
        facts=facts,
    )
    try:
        result = chat.compactor.compact(list(chat.messages), ctx)
    except Exception as exc:  # a failed compaction leaves the conversation as it was
        log.warn(f"compaction failed: {exc}")
        return False
    if result is None:
        return False
    chat.set_history(result.messages)
    chat.log.write(
        "compaction",
        strategy=result.strategy,
        reason=result.reason,
        tokens_before=result.tokens_before,
        tokens_after=result.tokens_after,
        masked=result.masked or None,
        summarized=list(result.summarized) if result.summarized else None,
        summary=result.summary or None,
        helper=result.helper or None,
        history=result.messages if result.strategy == "summarize" else None,
    )
    chat.log.sync()
    run_stats.count("compactions")
    run_stats.count(f"compaction_{result.strategy}")
    what = "Hid old tool results" if result.strategy == "mask" else "Summarized the earlier turns"
    run.hooks.ui("note", f"{what} to save context: about {result.tokens_before} -> {result.tokens_after} tokens.")
    return True


def _resume_from_log(chat: _Chat, slog: Any) -> tuple[bool, str]:
    """Rebuild the conversation from the session log. (restored, a note for the first message).

    A model API session gets its message list back (``replace_history``), up to the last
    complete turn. A session without one (the web chat) starts a new chat whose first
    message carries a summary of the log.
    """
    events = slog.events()
    projection = session_log_mod.history_from_events(events, slog.folder)
    chat.history = projection.tasks[-8:]
    if projection.has_wire and projection.messages and chat.messages is not None:
        chat.set_history(projection.messages)
        chat.map_sent = True
        slog.write("session_resumed", strategy="history", messages=len(projection.messages))
        note = "This session was resumed: crit restored the conversation from its log."
        if projection.unanswered_calls:
            note += " The last tool calls did not finish before crit stopped; check the state before repeating them."
        _ui("note", f"Resumed the conversation ({len(projection.messages)} messages).")
        return True, note
    summary = session_log_mod.resume_summary(events, slog.folder)
    slog.write("session_resumed", strategy="summary", messages=0)
    _ui("note", "Resumed: a new conversation starts with a summary of the session's log.")
    return False, summary


def _take_session_change(chat: _Chat, slog: Any) -> tuple[Any, str] | None:
    """After ``/resume`` or ``/fork`` in the UI: switch this conversation to that session's log.

    Returns (the new log, a note for the next message), or None when nothing was asked.
    """
    taker = getattr(ui.current(), "take_session_change", None)
    if not callable(taker):
        return None
    try:
        request = taker()
    except Exception:
        return None
    if not request:
        return None
    folder = Path(request.get("folder")) if isinstance(request, dict) and request.get("folder") else None
    if folder is None:
        return None
    try:
        new_log = session_log_mod.SessionLog.open(folder)
    except OSError as exc:
        _ui("bad", f"Could not open session {folder.name}: {exc}")
        return None
    if chat.can_restart:
        chat.restart(f"switching to session {folder.name}")  # recorded in the session being left
    slog.close(status="switched")
    chat.log = new_log
    restored, note = _resume_from_log(chat, new_log)
    chat.prefix = "" if restored else (_without_ready(chat.seed) if chat.seed else "")
    if not restored:
        chat.map_sent = False
    _announce_session(new_log)
    return new_log, note


def _announce_session(slog: Any) -> None:
    setter = getattr(ui.current(), "set_session", None)
    if callable(setter) and slog.enabled:
        try:
            setter(slog.folder.parent, slog.id)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"telling the UI about the session failed: {exc}")


def _open_session_log(
    home: BotHome,
    config: BotConfig,
    task: str,
    *,
    resume: str | None = None,
    settings: dict[str, Any] | None = None,
    kind: str = "agent",
) -> Any:
    """The session's event log in ``.crit/sessions``: a new one, or ``resume``'s (raises
    SessionNotFound). NullLog when ``"session_log": false`` or the folder cannot be written."""
    options = settings or {}
    if options.get("session_log") is False and not resume:
        return session_log_mod.NullLog()
    if resume:
        folder = session_log_mod.find_session(home.sessions_dir, resume)
        slog = session_log_mod.SessionLog.open(folder)
        tokens = slog.meta.get("tokens") if isinstance(slog.meta.get("tokens"), dict) else {}
        slog.meta["tokens_base"] = dict(tokens)
        return slog
    try:
        slog = session_log_mod.SessionLog.create(
            home.sessions_dir,
            title=task,
            kind=kind,
            model=config.model or "",
            backend=config.backend,
            cwd=str(home.root),
        )
    except OSError as exc:
        log.warn(f"no session log: {exc}")
        return session_log_mod.NullLog()
    slog.write(
        "session_started",
        id=slog.id,
        kind=kind,
        model=config.model or "",
        backend=config.backend,
        cwd=str(home.root),
        crit_version=slog.meta.get("crit_version") or None,
    )
    return slog


def _close_session_log(slog: Any, status: str) -> None:
    if not slog.enabled:
        return
    try:
        _update_session_meta(slog)
        slog.close(status=status)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"closing the session log failed: {exc}")


def _memory_notes(home: BotHome, settings: dict[str, Any]) -> str:
    """The memory files for the prefix (prompt_build.load_memory); a warning when over the cap."""
    if settings.get("memory_files") is False:
        return home.project_notes()
    try:
        memory = prompt_build.load_memory(home.root)
    except Exception as exc:  # the notes help; they must never stop a session
        log.warn(f"reading the memory files failed: {exc}")
        return home.project_notes()
    if memory.warning:
        _ui("note", memory.warning[:1].upper() + memory.warning[1:] + ".")
    return memory.text


def _session_settings(config: BotConfig, home: BotHome) -> dict[str, Any]:
    """Settings the loop takes from the run, not the settings file: the model API context window
    (compaction by pressure) and the first turn's session context (date, git status)."""
    values: dict[str, Any] = {"_first_turn_context": prompt_build.session_context(home.root)}
    if config.is_local:
        window = model_api.context_tokens(config)
        if window:
            values["_context_window"] = int(window)
    return values


def _update_session_meta(slog: Any) -> None:
    """Title, status, turns, and tokens in meta.json (what ``crit sessions`` lists)."""
    if not slog.enabled:
        return
    stats = run_stats.snapshot()
    base = slog.meta.get("tokens_base") if isinstance(slog.meta.get("tokens_base"), dict) else {}
    tokens = {
        key: int(base.get(key) or 0) + int(stats.get(name) or 0)
        for key, name in (("prompt", "prompt_tokens"), ("completion", "completion_tokens"), ("cached", "cached_tokens"))
    }
    slog.update_meta(turns=slog.turn, tokens=tokens)
    slog.sync()
