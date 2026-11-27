"""The open conversation: sends with retries, tracks its size, starts over when asked.

A web chat slows down and forgets the protocol as it grows, so it moves to a new chat by
size, turns and time. A model API session is compacted by context pressure instead
(engine/context.py); its message list is the engine's (see :attr:`_Chat.messages`).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import log, model_api, run_stats
from critique_bot import session_log as session_log_mod
from critique_bot import tool_api as agent_tools
from critique_bot.engine import runtime, ui
from critique_bot.engine.replies import _provider_error
from critique_bot.engine.text import _one_line
from critique_bot.model_api import ChatError, CloudflareBlocked, ReplyStalled
from critique_bot.prompt_build import without_ready as _without_ready


#: Protocol violations in a row that make the chat start over.
AMNESIA_VIOLATIONS = 2


#: A chat whose replies keep going wrong is left even when the violations are not in a row:
#: this many bad replies (cut off, made up, broken format, handed back, retried) among the
#: last HEALTH_WINDOW, and the next message goes to a new chat.
HEALTH_WINDOW = 6


HEALTH_BAD_REPLIES = 3


#: Cut-off replies in one chat before the next request for the rest goes to a new chat.
CUT_REPLIES_PER_CHAT = 2


#: A web chat gets slow and forgetful well before its context is full. These are the
#: sizes at which crit moves to a new chat in the middle of a task; between tasks it
#: moves at half of each.
DEFAULT_COMPACT_AFTER_CHARS = 120_000


DEFAULT_COMPACT_AFTER_TURNS = 30


DEFAULT_COMPACT_AFTER_MINUTES = 60


#: Share of the sizes above at which a new task starts in a new chat (cheap: only the task list is needed).
BOUNDARY_ROTATION_SHARE = 0.5


#: After this many failed tries in one chat, the next try goes to a new chat. A reply that
#: started and then stalled leaves a half-written answer behind, so that one moves at once.
RETRIES_BEFORE_NEW_CHAT = 1


DEFAULT_REPLY_RETRIES = 3


#: Seconds between resends when the chat page shows an error instead of a reply.
RETRY_DELAYS = (5.0, 15.0, 45.0)


# Overridable in tests so a retry does not really sleep.
_sleep = time.sleep


class _ProviderFailed(RuntimeError):
    """The chat page kept showing an error instead of a reply."""


def _stop_reply(session: Any) -> None:
    """Ask the chat page to stop a reply it is still writing. Best effort."""
    stop = getattr(session, "stop_generation", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception as exc:  # the next send waits for (or stops) it again
        log.debug(f"stopping the reply failed: {exc}")


class _Chat:
    """The open conversation: sends with retries, tracks its size, starts over when asked.

    A web chat slows down and forgets the protocol as it grows. Past
    ``compact_after`` characters, or after the model breaks the protocol
    ``AMNESIA_VIOLATIONS`` times in a row, the next message goes to a new
    chat with the instructions and a summary in front of it.
    """

    def __init__(
        self,
        session: Any,
        *,
        seed: str = "",
        retries: int = DEFAULT_REPLY_RETRIES,
        compact_after: int = DEFAULT_COMPACT_AFTER_CHARS,
        quiet: bool = False,
        compact_turns: int = DEFAULT_COMPACT_AFTER_TURNS,
        compact_minutes: int = DEFAULT_COMPACT_AFTER_MINUTES,
    ) -> None:
        self.session = session
        native = runtime.native_tools()
        if native is not None and callable(getattr(session, "use_tools", None)):
            session.use_tools(native)
        #: A helper tab's chat: no spinner and no notes in the terminal.
        self.quiet = quiet
        self.seed = (seed or "").strip()
        self.retries = max(0, retries)
        #: Where a failed send saves the page's screenshot and HTML before the retry (None: nowhere).
        self.failure_dir: Path | None = None
        self.compact_after = compact_after
        self.chars = 0
        self.violations = 0
        #: The last HEALTH_WINDOW replies, True for a bad one; and cut-off replies in this chat.
        self.health: deque[bool] = deque(maxlen=HEALTH_WINDOW)
        self.cut_replies = 0
        self.compact_turns = max(1, int(compact_turns or DEFAULT_COMPACT_AFTER_TURNS))
        self.compact_seconds = max(60, int(compact_minutes or DEFAULT_COMPACT_AFTER_MINUTES) * 60)
        #: Messages answered in this chat, and when it started.
        self.sends = 0
        self.born = time.monotonic()
        #: Set by the running task: what a new chat needs to carry on (see _TaskRun._resume_text).
        self.resume: Callable[[], str] | None = None
        self.map_sent = False
        #: Skills already sent in this chat; a new chat sends them again.
        self.skills_sent: set[str] = set()
        #: Sent ahead of the next message: the instructions, in a fresh chat.
        self.prefix = ""
        self.can_restart = callable(getattr(session, "new_chat", None))
        #: "task -> outcome" for each task in this session, for the summary.
        self.history: list[str] = []
        #: The session's event log (session_log.SessionLog); NullLog when there is none.
        self.log: Any = session_log_mod.NullLog()
        #: Model API sessions: compaction by context pressure (compaction.Compactor). The web
        #: chat has None and moves to a new chat by size, turns, and time instead.
        self.compactor: Any = None
        #: Opens a second session on the same model (one-off helper requests: the summary).
        self.side_factory: Callable[[], Any] | None = None
        #: Replies received in this session, across new chats (the session log's turn).
        self.replies = 0
        #: Notes for the next message: a mid-session change (role) on an append-only conversation.
        self.pending_notes: list[str] = []
        #: A model API session's conversation (OpenAI chat messages), owned by the engine: it
        #: is installed in the session before every request and grows by what the exchange
        #: added, and compaction and resume replace it. None for a web chat, whose page holds
        #: the conversation.
        self.messages: list[dict[str, Any]] | None = None
        if callable(getattr(session, "history", None)) and callable(getattr(session, "replace_history", None)):
            try:
                found = session.history()
            except Exception:  # noqa: BLE001 - a session that cannot say starts empty
                found = []
            self.messages = [dict(item) for item in found] if isinstance(found, list) else []

    @property
    def last_detail(self) -> Any:
        return getattr(self.session, "last_detail", None)

    def send(self, payload: str) -> str:
        """Send and return the reply, retrying when the page fails or never answers.

        The first retry goes to the same chat. After that the conversation
        itself may be broken, so the next try opens a new chat with the
        instructions and the running task's summary in front of the message.
        """
        attempt = 0
        moved = False
        # A retry in the same chat resends exactly the same text, instructions included:
        # a message that never went out must not lose them.
        prefix, self.prefix = self.prefix, ""
        while True:
            outgoing = prefix + "\n\n" + payload if prefix else payload
            outgoing, hidden = agent_tools.redact_secrets(outgoing)
            if hidden:
                self._note("note", f"Kept {hidden} secret{'s' if hidden != 1 else ''} out of the message to the chat.")
            if not self.quiet:
                ui._waiting(True)
            stalled = False
            self.install_history()
            mark = self.log.request(self.session, outgoing, attempt=attempt or None)
            try:
                reply = self.session.send(outgoing)
                problem = _provider_error(reply)
            except CloudflareBlocked:
                raise  # only a visible window gets past it; the session reopens one
            except ChatError as exc:
                reply, problem = "", f"the chat page failed: {_one_line(str(exc), 160)}"
                self.log.write("error", message=problem, attempt=attempt)
                stalled = isinstance(exc, ReplyStalled)
                if self.failure_dir is not None:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    folder = self.failure_dir / f"{stamp}-attempt{attempt + 1}"
                    model_api.save_failure(self.session, folder)
                    log.info(f"saved what the chat page showed to {folder}")
            finally:
                if not self.quiet:
                    ui._waiting(False)
            self.chars += len(outgoing) + len(reply or "")
            if problem is None:
                self.sends += 1
                self.replies += 1
                self.take_history()
                self.log.reply(self.session, mark, reply)
                return reply
            self.note_reply(bad=True)
            if attempt >= self.retries:
                raise _ProviderFailed(problem)
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            attempt += 1
            # A timed-out reply may still be streaming; resending over it
            # duplicates the message and crosses the two answers.
            _stop_reply(self.session)
            # A stalled reply left a half-written answer in the chat, and an unhealthy chat
            # has already shown it keeps going wrong: neither is worth another try in place.
            move_now = stalled or self.unhealthy()
            if (attempt > RETRIES_BEFORE_NEW_CHAT or move_now) and not moved and self.can_restart:
                if self.restart("the chat stopped answering"):
                    moved = True
                    prefix, self.prefix = self.prefix, ""
                    summary = ""
                    if callable(self.resume):
                        try:
                            summary = self.resume() or ""
                        except Exception as exc:  # the retry still goes out without the summary
                            log.debug(f"could not build the summary for the new chat: {exc}")
                    if summary:
                        payload = summary + "\n\n" + payload
                    self._note("note", f"{problem[:1].upper()}{problem[1:]}. Trying again in a new chat ({attempt}/{self.retries}).")
                    _sleep(min(delay, 5.0))
                    continue
            self._note("note", f"{problem[:1].upper()}{problem[1:]}. Sending again in {delay:.0f}s ({attempt}/{self.retries}).")
            _sleep(delay)

    def install_history(self) -> None:
        """Give the session the engine's message list before a request (a model API session)."""
        if self.messages is not None:
            self.session.replace_history(self.messages)

    def take_history(self) -> None:
        """Add what the exchange appended to the session's list (the request and the reply)."""
        if self.messages is None:
            return
        found = self.session.history()
        if isinstance(found, list) and len(found) >= len(self.messages):
            self.messages.extend(dict(item) for item in found[len(self.messages):])
        elif isinstance(found, list):  # the session shortened its list itself: follow it
            self.messages = [dict(item) for item in found]

    def set_history(self, messages: list[dict[str, Any]]) -> None:
        """Replace the conversation (compaction, resume); the session gets it with the next request."""
        if self.messages is None:
            return
        self.messages = [dict(item) for item in messages if isinstance(item, dict)]
        self.session.replace_history(self.messages)

    def _note(self, kind: str, message: str) -> None:
        if not self.quiet:
            ui._ui(kind, message)

    def too_long(self) -> bool:
        return self.can_restart and bool(self.rotation_reason(size_only=True))

    def stale_for_next_task(self) -> bool:
        """Past half its size: a new task is better off in a new chat (only the task list is needed)."""
        return self.can_restart and bool(self.rotation_reason(size_only=True, share=BOUNDARY_ROTATION_SHARE))

    def note_reply(self, *, bad: bool) -> None:
        """Remember whether the latest reply (or try) went wrong, for :meth:`unhealthy`."""
        self.health.append(bad)

    def strike(self) -> None:
        """A protocol violation: counts toward amnesia (in a row) and toward the chat's health."""
        self.violations += 1
        self.note_reply(bad=True)

    def good(self) -> None:
        """A reply that followed the protocol: the in-a-row count starts over."""
        self.violations = 0
        self.note_reply(bad=False)

    def note_cut(self) -> None:
        """A reply that was cut off before it finished."""
        self.cut_replies += 1
        self.note_reply(bad=True)

    def unhealthy(self) -> bool:
        """Bad replies keep coming, even with good ones between them."""
        return sum(1 for bad in self.health if bad) >= HEALTH_BAD_REPLIES

    def rotation_reason(self, *, size_only: bool = False, share: float = 1.0) -> str:
        """Why this chat should move to a new one now, or "". ``share`` scales the size limits.

        With a compactor (a model API session) size, turns, and time never move the chat:
        context pressure compacts it in place instead.
        """
        if self.compactor is not None:
            if size_only:
                return ""
        elif self.chars > self.compact_after * share:
            return "the chat grew long"
        elif self.sends >= max(1, int(self.compact_turns * share)):
            return f"the chat reached {self.sends} messages"
        elif self.sends and (time.monotonic() - self.born) >= self.compact_seconds * share:
            return f"the chat has been open {(time.monotonic() - self.born) / 60:.0f} minutes"
        if size_only:
            return ""
        if self.violations >= AMNESIA_VIOLATIONS:
            return "the model lost track of the instructions"
        if self.cut_replies >= CUT_REPLIES_PER_CHAT:
            return "replies keep getting cut off in this chat"
        if self.unhealthy():
            return "this chat keeps giving broken replies"
        return ""

    def wants_restart(self) -> bool:
        return self.can_restart and bool(self.rotation_reason())

    def restart(self, reason: str) -> bool:
        """Open a new conversation. The instructions go in front of the next message."""
        if not self.can_restart:
            return False
        try:
            started = self.session.new_chat()
        except Exception as exc:  # the old chat still works; keep using it
            log.warn(f"could not open a new chat: {exc}")
            started = False
        if started is False:
            self.can_restart = False
            return False
        self._note("note", f"Starting a new conversation: {reason}.")
        run_stats.count("compactions")
        self.log.write("compaction", strategy="new_chat", reason=reason)
        self.log.sync()
        if self.messages is not None:
            self.messages = []
        self.chars = 0
        self.violations = 0
        self.health.clear()
        self.cut_replies = 0
        self.sends = 0
        self.born = time.monotonic()
        self.map_sent = False
        self.skills_sent = set()
        self.prefix = _without_ready(self.seed) if self.seed else ""
        if self.history:
            earlier = "EARLIER IN THIS SESSION (tasks done in the previous chat, newest last):\n- " + "\n- ".join(
                self.history[-8:]
            )
            self.prefix = (self.prefix + "\n\n" + earlier).strip()
        return True
