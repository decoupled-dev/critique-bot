# SPDX-License-Identifier: Apache-2.0
"""The Engine API: sessions as objects with an event stream and answerable requests.

``Engine`` creates, opens (resume), forks and lists the sessions of one project.
A ``SessionHandle`` runs its session on a worker thread with :func:`ui.use_thread_ui`
set to an :class:`EventUI`: everything the loop shows becomes an event in the session
log, and everything it asks (approvals, questions, plan review) becomes a ``request``
event with an id that a client answers with :meth:`SessionHandle.answer`. Front ends in
the same process (the TUI) or in another one (``crit serve`` clients, ACP, ``crit run
--json``) all read the same events.

Events are the session log's (``session_log.EVENT_TYPES``), with their ``seq``: a client
that lost its connection asks for ``events(since_seq=N)`` and misses nothing, and after a
crash ``open_session`` resumes from the same log (Stage 3).
"""

from __future__ import annotations

import bisect
import dataclasses
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from critique_bot import log, ui_api
from critique_bot import session_log as session_log_mod
from critique_bot.config import BotConfig
from critique_bot.project_home import BotHome

#: How a client answers each kind of request.
PERMISSION_DECISIONS = ("yes", "always", "no")
PLAN_DECISIONS = ("auto", "edits", "ask", "no", "none")
REQUEST_KINDS = ("permission", "question", "plan_review")
#: Session states a client sees.
IDLE, RUNNING, WAITING, CLOSED = "idle", "running", "waiting", "closed"


class EngineError(RuntimeError):
    """A request the engine cannot do (unknown session, a closed session, a bad folder)."""


class RequestClosed(EngineError):
    """The request is unknown or was answered already: only one answer per request counts."""


class BadReply(EngineError):
    """The reply does not fit the request (an unknown decision, a missing text)."""


@dataclasses.dataclass
class _Pending:
    kind: str
    #: The answer when no one gives one (unattended, cancelled): what NullUI answers.
    default: dict[str, Any]
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    reply: Any = None


class _Cancelled(KeyboardInterrupt):
    """Raised on the session's thread to stop the running task (the loop treats it as Ctrl+C)."""


class EventUI(ui_api.NullUI):
    """The UI of one Engine session: output goes to the log as events; questions become requests."""

    def __init__(self, handle: "SessionHandle") -> None:
        super().__init__()
        self.handle = handle
        self.in_task = False

    # session
    def configure(self, **settings: Any) -> None:
        return None

    def approve_mode(self) -> str:
        return self.handle.mode

    def normalize_mode(self, raw: Any, default: str = "ask") -> str:
        return ui_api.normalize_mode(raw, default)

    # output
    def note(self, kind: str, message: str) -> None:
        if kind == "task":
            self.in_task = True
        self.handle._write("notice", kind=kind, text=message)

    def final_status(self, code: str, *, ok: bool) -> None:
        self.in_task = False

    def assistant(self, text: str) -> None:
        self.handle._write("assistant_text", text=text)

    def tool_start(self, name: str, arguments: dict[str, Any]) -> None:
        self._check_cancel()

    def tool_output(self, text: str) -> None:
        self.handle._live("tool_output", text=text)

    def show_diff(self, diff: str) -> bool:
        """The task's diff on disk goes to the clients as a notice (not to the server's terminal)."""
        self.handle._write("notice", kind="diff", text=diff)
        return True

    def waiting(self, active: bool, label: str = "") -> None:
        self._check_cancel()

    # questions
    def read_message(self) -> str | None:
        self.in_task = False
        return self.handle._next_message()

    def approve(self, permission: Any, *, risky: str = "") -> tuple[str, str]:
        reply = self._ask(
            "permission",
            {
                "perm_kind": str(getattr(permission, "kind", "") or ""),
                "summary": str(getattr(permission, "summary", "") or ""),
                "detail": str(getattr(permission, "detail", "") or ""),
                "risky": risky or None,
                "options": list(PERMISSION_DECISIONS),
            },
            default={"decision": "no", "reason": "no one to approve" + (f" a command that {risky}" if risky else "")},
        )
        return str(reply.get("decision") or "no"), str(reply.get("reason") or "")

    def review_plan(self) -> tuple[str, str]:
        reply = self._ask("plan_review", {"options": list(PLAN_DECISIONS)}, default={"decision": "none"})
        return str(reply.get("decision") or "none"), str(reply.get("feedback") or "")

    def ask_question(self, question: str, options: list[str] | None = None) -> str | None:
        reply = self._ask("question", {"text": question, "options": list(options or []) or None}, default={})
        text = reply.get("text")
        return str(text) if text is not None else None

    def _ask(self, kind: str, fields: dict[str, Any], *, default: dict[str, Any]) -> dict[str, Any]:
        self._check_cancel()
        reply = self.handle._request(kind, fields, default=default)
        self._check_cancel()
        return reply

    def _check_cancel(self) -> None:
        if self.in_task and self.handle._take_cancel():
            raise _Cancelled()


class SessionHandle:
    """One session of the engine. Thread-safe: several clients may watch and answer it."""

    def __init__(
        self,
        engine: "Engine",
        slog: Any,
        *,
        config: BotConfig,
        role: str | None,
        mode: str,
        resume: bool,
        unattended: bool = False,
        max_rounds: int | None = None,
    ) -> None:
        self.engine = engine
        self.log = slog
        self.id = str(slog.id)
        self.folder = Path(slog.folder)
        self.config = config
        self.role = role
        self.mode = ui_api.normalize_mode(mode)
        self.unattended = unattended
        self.max_rounds = max_rounds
        self._resume = resume
        self._cond = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._seqs: list[int] = []
        self._live_events: deque[dict[str, Any]] = deque(maxlen=200)
        self._inbox: deque[str | None] = deque()
        self._pending: dict[str, _Pending] = {}
        self._cancel = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self.state = IDLE
        self.exit_code: int | None = None
        # The listener first, then the file: an event written in between is in one of them.
        slog.add_listener(self._on_event)
        for event in session_log_mod.read_events(self.folder):
            self._add(event)

    # ------------------------------------------------------------------ events

    def _on_event(self, event: dict[str, Any]) -> None:
        self._add(event)

    def _add(self, event: dict[str, Any]) -> None:
        seq = event.get("seq")
        if not isinstance(seq, int):
            return
        with self._cond:
            index = bisect.bisect_left(self._seqs, seq)
            if index < len(self._seqs) and self._seqs[index] == seq:
                return  # read from the file and heard from the writer: once is enough
            self._seqs.insert(index, seq)
            self._events.insert(index, event)
            self._cond.notify_all()

    def _write(self, event_type: str, **fields: Any) -> dict[str, Any]:
        return self.log.write(event_type, **fields)

    def _live(self, kind: str, **fields: Any) -> None:
        """An event that is not kept (tool output as it streams): live watchers only, no seq."""
        with self._cond:
            self._live_events.append({"type": kind, **fields})
            self._cond.notify_all()

    def events(self, since_seq: int = 0, *, follow: bool = True, stop: threading.Event | None = None,
               idle_timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """The session's events after ``since_seq``, then new ones as they come (``follow``).

        Ends when the session is closed and every event was given, when ``stop`` is set, or
        when nothing came for ``idle_timeout`` seconds.
        """
        last = since_seq
        quiet_since = time.monotonic()
        while True:
            with self._cond:
                start = bisect.bisect_right(self._seqs, last)
                batch = self._events[start:]
                if not batch:
                    if not follow or self._closed or (stop is not None and stop.is_set()):
                        return
                    if idle_timeout is not None and time.monotonic() - quiet_since >= idle_timeout:
                        return
                    self._cond.wait(timeout=0.25)
                    continue
            quiet_since = time.monotonic()
            for event in batch:
                last = int(event["seq"])
                yield event

    def last_seq(self) -> int:
        with self._cond:
            return self._seqs[-1] if self._seqs else 0

    def pending_requests(self) -> list[dict[str, Any]]:
        """The open requests, as their request events."""
        with self._cond:
            open_ids = {rid for rid, item in self._pending.items() if not item.done.is_set()}
            return [event for event in self._events if event.get("type") == "request" and event.get("id") in open_ids]

    # ------------------------------------------------------------------ requests

    def _request(self, kind: str, fields: dict[str, Any], *, default: dict[str, Any]) -> dict[str, Any]:
        rid = uuid.uuid4().hex[:12]
        pending = _Pending(kind, default)
        with self._cond:
            self._pending[rid] = pending
            self.state = WAITING
        self._write("request", id=rid, kind=kind, **{k: v for k, v in fields.items() if v is not None})
        if self.unattended:
            self._settle(rid, default, by="unattended")
        pending.done.wait()
        with self._cond:
            self._pending.pop(rid, None)
            if self.state == WAITING and not any(not p.done.is_set() for p in self._pending.values()):
                self.state = RUNNING
        reply = pending.reply
        return reply if isinstance(reply, dict) else default

    def answer(self, request_id: str, reply: Any, *, by: str = "client") -> None:
        """Answer an open request. Raises RequestClosed when it is unknown or already answered."""
        with self._cond:
            pending = self._pending.get(request_id)
            if pending is None or pending.done.is_set():
                raise RequestClosed(f"request {request_id} is not open (unknown, or answered already)")
            reply = _check_reply(pending.kind, reply)
        self._settle(request_id, reply, by=by)

    def _settle(self, request_id: str, reply: dict[str, Any], *, by: str) -> None:
        with self._cond:
            pending = self._pending.get(request_id)
            if pending is None or pending.done.is_set():
                raise RequestClosed(f"request {request_id} is not open (unknown, or answered already)")
            pending.reply = reply
            pending.done.set()
        self._write("request_answered", id=request_id, reply=reply, by=by)

    # ------------------------------------------------------------------ control

    def send(self, text: str, *, attachments: tuple[Any, ...] = (), close_after: bool = False) -> None:
        """Start a task, or queue it for when the running one ends. ``close_after``: end the
        session when that task is done (a headless run)."""
        text = str(text or "").strip()
        if attachments:
            text += "\n\n" + "\n\n".join(str(item) for item in attachments)
        if not text:
            raise BadReply("a message needs text")
        with self._cond:
            if self._closed:
                raise EngineError(f"session {self.id} is closed")
            started = self._thread is not None
            if started:
                self._inbox.append(text)
                if close_after:
                    self._inbox.append(None)
                self._cond.notify_all()
            else:
                if close_after:
                    self._inbox.append(None)
                self.state = RUNNING
                self._thread = threading.Thread(target=self._run, args=(text,), name=f"crit-session-{self.id}",
                                                daemon=True)
        if not started:
            self._thread.start()

    def _next_message(self) -> str | None:
        with self._cond:
            if self.state != CLOSED:
                self.state = IDLE
            while not self._inbox:
                self._cond.wait(timeout=0.5)
            item = self._inbox.popleft()
            if item is not None:
                self.state = RUNNING
                self._cancel = False
            return item

    def cancel(self) -> bool:
        """Stop the running task (it ends as INTERRUPTED; the session stays open). False when idle."""
        with self._cond:
            if self.state not in {RUNNING, WAITING}:
                return False
            self._cancel = True
            waiting = [rid for rid, item in self._pending.items() if not item.done.is_set()]
        for rid in waiting:
            with self._cond:
                pending = self._pending.get(rid)
            if pending is None:
                continue
            try:
                self._settle(rid, {**pending.default, "reason": "cancelled"} if pending.kind == "permission"
                             else pending.default, by="cancel")
            except RequestClosed:
                pass
        return True

    def _take_cancel(self) -> bool:
        with self._cond:
            if self._cancel:
                self._cancel = False
                return True
            return False

    def set_mode(self, mode: str) -> str:
        """Switch the permission mode; the running task picks it up at its next step."""
        new = ui_api.normalize_mode(mode, "")
        if new not in ui_api.MODES:
            raise BadReply(f"mode must be one of {', '.join(ui_api.MODES)}")
        self.mode = new
        return new

    def undo(self, checkpoint: str | None = None) -> list[str]:
        """Restore the files the last task changed (its undo checkpoint). Only while idle."""
        from critique_bot.agent_edit import undo_last

        if self.state in {RUNNING, WAITING}:
            raise EngineError("a task is running; cancel it first")
        if checkpoint:
            raise BadReply("only the last checkpoint can be restored")
        return undo_last(self.engine.home.cache_dir, self.engine.home.root)

    def close(self, *, wait: float | None = 10.0) -> None:
        """End the session after the running task (or now when idle)."""
        with self._cond:
            if self._closed:
                return
            self._inbox.append(None)
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait)
        with self._cond:
            if thread is None:
                self._finish(status="closed")  # never ran: nothing else closes its log

    def info(self) -> dict[str, Any]:
        meta = session_log_mod.read_meta(self.folder)
        return {
            "id": self.id,
            "title": str(meta.get("title") or ""),
            "state": self.state,
            "mode": self.mode,
            "agent": self.role or "",
            "created": str(meta.get("created") or ""),
            "updated": str(meta.get("updated") or ""),
            "status": str(meta.get("status") or ""),
            "parent": str(meta.get("parent") or ""),
            "last_seq": self.last_seq(),
            "live": True,
            "pending": [{"id": event.get("id"), "kind": event.get("kind"), "seq": event.get("seq")}
                        for event in self.pending_requests()],
        }

    # ------------------------------------------------------------------ the worker

    def _run(self, first: str) -> None:
        from critique_bot.engine import runs, ui

        ui.use_thread_ui(EventUI(self))
        code = 1
        try:
            code = runs.run_agent(
                self.config,
                self.engine.home,
                first,
                max_rounds=self.max_rounds,
                output_dir=None,
                headed=self.engine.headed,
                approve_mode=self.mode,
                role=self.role,
                resume=self.id if self._resume else None,
                session_log=self.log,
                shared_process=True,
            )
        except BaseException as exc:  # noqa: BLE001 - the thread must record why it ended
            log.warn(f"session {self.id} stopped: {exc}")
        finally:
            ui.use_thread_ui(None)
            self.exit_code = code
            with self._cond:
                self._finish()

    def _finish(self, *, status: str | None = None) -> None:
        """The session is over: the last event, the log closed (run_agent already set its status)."""
        if self._closed:
            return
        self._closed = True
        self.state = CLOSED
        try:
            self.log.write("session_closed", code=self.exit_code)
            self.log.close(status=status)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"closing session {self.id}: {exc}")
        self.log.remove_listener(self._on_event)
        self._cond.notify_all()
        self.engine._forget(self)


def _check_reply(kind: str, reply: Any) -> dict[str, Any]:
    """The reply as the loop takes it, or BadReply."""
    if isinstance(reply, str):
        reply = {"text": reply} if kind == "question" else {"decision": reply}
    if not isinstance(reply, dict):
        raise BadReply("the reply must be an object")
    if kind == "permission":
        decision = str(reply.get("decision") or "").lower()
        if decision not in PERMISSION_DECISIONS:
            raise BadReply(f"decision must be one of {', '.join(PERMISSION_DECISIONS)}")
        return {"decision": decision, "reason": str(reply.get("reason") or "")}
    if kind == "plan_review":
        decision = str(reply.get("decision") or "").lower()
        if decision not in PLAN_DECISIONS:
            raise BadReply(f"decision must be one of {', '.join(PLAN_DECISIONS)}")
        return {"decision": decision, "feedback": str(reply.get("feedback") or "")}
    text = reply.get("text", reply.get("answer"))
    if text is None:
        raise BadReply("a question's reply needs text")
    return {"text": str(text)}


class Engine:
    """The sessions of one project (``home``) on one model configuration."""

    def __init__(self, config: BotConfig, home: BotHome, *, headed: bool = False) -> None:
        self.config = config
        self.home = home
        self.headed = headed
        self._lock = threading.Lock()
        #: Held while a logged session is opened, so two clients cannot open it twice.
        self._open_lock = threading.RLock()
        self._sessions: dict[str, SessionHandle] = {}

    # ------------------------------------------------------------------ sessions

    def create_session(
        self,
        cwd: str | Path | None = None,
        *,
        agent: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        title: str = "",
        unattended: bool = False,
        max_rounds: int | None = None,
        kind: str = "agent",
    ) -> SessionHandle:
        """A new session (its log exists at once, so the id is known before the first message)."""
        from critique_bot.engine.context import _open_session_log
        from critique_bot.engine.loop import effective_settings

        self._check_cwd(cwd)
        config = self._config(model)
        settings = effective_settings(self.home.settings)
        slog = _open_session_log(self.home, config, title, settings={**settings, "session_log": True}, kind=kind)
        if not getattr(slog, "enabled", False):
            raise EngineError("the session log could not be written; the Engine API needs it (.crit/sessions)")
        handle = SessionHandle(self, slog, config=config, role=agent, mode=self._mode(mode, settings),
                               resume=False, unattended=unattended, max_rounds=max_rounds)
        return self._keep(handle)

    def open_session(self, session_id: str, *, mode: str | None = None, unattended: bool = False) -> SessionHandle:
        """A logged session to continue (resume): the live one, or the log reopened."""
        with self._open_lock:
            return self._open(session_id, mode=mode, unattended=unattended)

    def _open(self, session_id: str, *, mode: str | None, unattended: bool) -> SessionHandle:
        from critique_bot.engine.context import _open_session_log
        from critique_bot.engine.loop import effective_settings

        live = self.session(session_id)
        if live is not None:
            return live
        try:
            folder = session_log_mod.find_session(self.home.sessions_dir, session_id)
        except session_log_mod.SessionNotFound as exc:
            raise EngineError(str(exc)) from exc
        live = self.session(folder.name)  # session_id was a prefix of a live one
        if live is not None:
            return live
        settings = effective_settings(self.home.settings)
        meta = session_log_mod.read_meta(folder)
        pid = meta.get("pid")
        if meta.get("status") == "running" and pid != os.getpid() and session_log_mod._alive(pid):
            raise EngineError(f"session {folder.name} is running in another crit (pid {pid})")
        slog = _open_session_log(self.home, self.config, "", resume=folder.name, settings=settings)
        handle = SessionHandle(self, slog, config=self._config(str(meta.get("model") or "") or None),
                               role=None, mode=self._mode(mode, settings), resume=True, unattended=unattended)
        return self._keep(handle)

    def fork_session(self, session_id: str, at_seq: int | None = None, *, mode: str | None = None) -> SessionHandle:
        """A copy of a session up to ``at_seq`` (all of it when None), opened to continue."""
        try:
            fork = session_log_mod.fork_session(self.home.sessions_dir, session_id, at=at_seq)
        except session_log_mod.SessionNotFound as exc:
            raise EngineError(str(exc)) from exc
        return self.open_session(fork.id, mode=mode)

    def list_sessions(self, cwd: str | Path | None = None) -> list[dict[str, Any]]:
        """Every logged session of the project, newest first; live ones with their state."""
        self._check_cwd(cwd)
        rows = []
        for row in session_log_mod.list_sessions(self.home.sessions_dir):
            live = self.session(row.id)
            if live is not None:
                rows.append(live.info())
                continue
            rows.append({"id": row.id, "title": row.title, "state": CLOSED, "status": row.status,
                         "created": row.created, "updated": row.updated, "parent": row.parent,
                         "turns": row.turns, "tokens": row.tokens, "live": False})
        return rows

    def session(self, session_id: str) -> SessionHandle | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_live(self) -> list[SessionHandle]:
        with self._lock:
            return list(self._sessions.values())

    def find(self, session_id: str) -> str:
        """The full id of a live or logged session (a unique prefix works), or EngineError."""
        if self.session(session_id) is not None:
            return session_id
        try:
            return session_log_mod.find_session(self.home.sessions_dir, session_id).name
        except session_log_mod.SessionNotFound as exc:
            raise EngineError(str(exc)) from exc

    def events(self, session_id: str, since_seq: int = 0, *, follow: bool = True,
               stop: threading.Event | None = None) -> Iterator[dict[str, Any]]:
        """A session's events: live ones follow; a closed session's come from its log, once."""
        live = self.session(session_id)
        if live is not None:
            yield from live.events(since_seq, follow=follow, stop=stop)
            return
        try:
            folder = session_log_mod.find_session(self.home.sessions_dir, session_id)
        except session_log_mod.SessionNotFound as exc:
            raise EngineError(str(exc)) from exc
        for event in session_log_mod.read_events(folder):
            if int(event.get("seq") or 0) > since_seq:
                yield event

    def close(self) -> None:
        with self._lock:
            handles = list(self._sessions.values())
        for handle in handles:
            handle.close(wait=5.0)

    # ------------------------------------------------------------------ helpers

    def _keep(self, handle: SessionHandle) -> SessionHandle:
        with self._lock:
            self._sessions[handle.id] = handle
        return handle

    def _forget(self, handle: SessionHandle) -> None:
        with self._lock:
            if self._sessions.get(handle.id) is handle:
                del self._sessions[handle.id]

    def _config(self, model: str | None) -> BotConfig:
        return dataclasses.replace(self.config, model=model) if model else self.config

    @staticmethod
    def _mode(mode: str | None, settings: dict[str, Any]) -> str:
        return ui_api.normalize_mode(mode or settings.get("permissions") or settings.get("mode"))

    def _check_cwd(self, cwd: str | Path | None) -> None:
        if cwd is None or str(cwd) == "":
            return
        try:
            same = Path(cwd).resolve() == Path(self.home.root).resolve()
        except OSError:
            same = False
        if not same:
            raise EngineError(f"this engine serves {self.home.root}; start another for {cwd}")


# --------------------------------------------------------------------------- driving a front end


def drive_ui(
    events: Iterator[dict[str, Any]],
    front: Any,
    *,
    answer: Any,
    send: Any = None,
    stop_after_task: bool = False,
    replay_until: int = 0,
    open_requests: Any = (),
) -> str:
    """Show a session's events on a front end with NullUI's methods, and answer its requests.

    ``events`` is a session's event stream (in process: ``SessionHandle.events``; remote:
    the server's SSE). Each ``request`` is put to the front end (``approve``, ``ask_question``,
    ``review_plan``) and its answer goes back through ``answer(request_id, reply)``. After a
    task finishes, ``front.read_message()`` gives the next one to ``send`` (None ends).
    Returns the last finish code. Events up to ``replay_until`` (an attach replaying the
    history) are shown but ask nothing, except the requests in ``open_requests`` (still
    waiting when the client attached).
    """
    code = ""
    answered: set[str] = set()
    for event in events:
        kind = event.get("type")
        if kind == "notice":
            if event.get("kind") == "diff":
                shower = getattr(front, "show_diff", None)
                if not (callable(shower) and shower(str(event.get("text") or ""))):
                    front.note("good", "On disk, this task changed:\n" + str(event.get("text") or "")[:4000])
            else:
                front.note(str(event.get("kind") or "note"), str(event.get("text") or ""))
        elif kind == "assistant_text":
            front.assistant(str(event.get("text") or ""))
        elif kind == "tool_started":
            front.tool_start(str(event.get("tool") or ""), event.get("arguments") or {})
        elif kind == "tool_result":
            result = event.get("result") if isinstance(event.get("result"), dict) else {"ok": event.get("ok")}
            front.tool_done(str(event.get("tool") or ""), {}, result)
        elif kind == "request_answered":
            answered.add(str(event.get("id")))
        elif kind == "request" and str(event.get("id")) not in answered and (
                int(event.get("seq") or 0) > replay_until or str(event.get("id")) in open_requests):
            reply = _front_reply(front, event)
            try:
                answer(str(event.get("id")), reply)
            except RequestClosed:
                pass  # another client answered first
        elif kind == "task_finished":
            code = str(event.get("code") or "")
            front.final_status(code, ok=code in {"COMPLETED", "FINISHED", "DONE"})
            if stop_after_task:
                return code
            if send is not None and int(event.get("seq") or 0) > replay_until:
                text = front.read_message()
                if text is None:
                    return code
                send(text)
        elif kind == "session_closed":
            return code
    return code


def _front_reply(front: Any, event: dict[str, Any]) -> dict[str, Any]:
    kind = event.get("kind")
    if kind == "permission":
        stand_in = _PermissionView(event)
        decision, reason = front.approve(stand_in, risky=str(event.get("risky") or ""))
        return {"decision": decision if decision in PERMISSION_DECISIONS else "no", "reason": reason}
    if kind == "plan_review":
        decision, feedback = front.review_plan()
        return {"decision": decision if decision in PLAN_DECISIONS else "none", "feedback": feedback}
    text = front.ask_question(str(event.get("text") or ""), event.get("options"))
    return {"text": text or ""}


class _PermissionView:
    """A permission request as the front end's approve() reads it (kind, summary, detail)."""

    def __init__(self, event: dict[str, Any]) -> None:
        self.kind = str(event.get("perm_kind") or "")
        self.summary = str(event.get("summary") or "")
        self.detail = str(event.get("detail") or "")
        self.key = ""
        self.risk = str(event.get("risky") or "")
